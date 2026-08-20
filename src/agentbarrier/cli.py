"""Command-line interface."""

from __future__ import annotations

import argparse
import getpass
import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from agentbarrier import __version__
from agentbarrier.adapter import AgentAdapter
from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.compatibility import (
    SELECTABLE_ADAPTER_SPECS,
    dump_compatibility_evidence,
    evidence_has_errors,
    generate_compatibility_evidence,
    select_adapter_specs,
    write_compatibility_outputs,
)
from agentbarrier.errors import AgentBarrierError
from agentbarrier.models import ApprovalBarrierProfile, JsonValue
from agentbarrier.models import Decision as RuntimeDecision
from agentbarrier.reporters import render_console, write_json, write_junit, write_sarif
from agentbarrier.runner import RunnerOptions, SuiteRunner
from agentbarrier.runtime.models import RuntimeReconciliation, RuntimeStatus
from agentbarrier.runtime.serialization import action_payload, receipt_payload
from agentbarrier.runtime.store import SQLiteRuntimeStore
from agentbarrier.scenarios import DEFAULT_SCENARIOS


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""

    parser = argparse.ArgumentParser(
        prog="agentbarrier",
        description="Verify control-plane safety guarantees for AI-agent tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="verify an importable adapter")
    verify.add_argument("target", help="adapter instance, class, or factory as MODULE:ATTRIBUTE")
    _add_run_options(verify)

    self_test = commands.add_parser("self-test", help="verify the safe reference adapter")
    _add_run_options(self_test)

    compatibility = commands.add_parser(
        "compatibility",
        help="generate deterministic framework compatibility evidence",
    )
    compatibility.add_argument(
        "--adapter",
        action="append",
        choices=[spec.key for spec in SELECTABLE_ADAPTER_SPECS],
        help=(
            "include one bundled adapter; repeat to select multiple "
            "(default: mutually compatible set)"
        ),
    )
    compatibility.add_argument(
        "--profile",
        action="append",
        choices=[profile.value for profile in ApprovalBarrierProfile],
        help="include one approval profile; repeat to select multiple (default: both)",
    )
    compatibility.add_argument("--json", metavar="PATH", help="write compatibility JSON")
    compatibility.add_argument(
        "--markdown",
        metavar="PATH",
        help="update the generated compatibility section in a Markdown file",
    )
    compatibility.add_argument(
        "--check",
        action="store_true",
        help="check selected output files for drift instead of updating them",
    )
    compatibility.add_argument(
        "--strict-missing",
        action="store_true",
        help="fail when a selected adapter distribution is unavailable",
    )
    compatibility.add_argument("--settle", type=float, default=0.01)
    compatibility.add_argument("--operation-timeout", type=float, default=5.0)
    compatibility.add_argument("--tool-timeout", type=float, default=0.05)
    compatibility.set_defaults(handler=_run_compatibility)

    scenarios = commands.add_parser("scenarios", help="list built-in guarantee scenarios")
    scenarios.set_defaults(handler=_list_scenarios)

    approvals = commands.add_parser("approvals", help="inspect and decide runtime approvals")
    approval_commands = approvals.add_subparsers(dest="approval_command", required=True)

    approval_list = approval_commands.add_parser("list", help="list runtime actions")
    _add_runtime_db_option(approval_list)
    approval_list.add_argument(
        "--status",
        choices=[status.value for status in RuntimeStatus],
        help="include only actions in one lifecycle state",
    )
    approval_list.add_argument("--json", action="store_true", help="write JSON to stdout")
    approval_list.set_defaults(handler=_run_approvals_list)

    approval_show = approval_commands.add_parser("show", help="show one exact runtime action")
    approval_show.add_argument("action_id")
    _add_runtime_db_option(approval_show)
    approval_show.add_argument("--json", action="store_true", help="write JSON to stdout")
    approval_show.set_defaults(handler=_run_approvals_show)

    for name, decision in (
        ("approve", RuntimeDecision.APPROVE),
        ("reject", RuntimeDecision.REJECT),
    ):
        decision_parser = approval_commands.add_parser(name, help=f"{name} a pending action")
        decision_parser.add_argument("action_id")
        _add_runtime_db_option(decision_parser)
        decision_parser.add_argument(
            "--decided-by",
            required=True,
            help="reviewer identity recorded in the audit receipt",
        )
        decision_parser.add_argument("--reason", help="optional decision reason")
        decision_parser.set_defaults(handler=_run_approval_decision, decision=decision)

    reconcile = approval_commands.add_parser(
        "reconcile", help="resolve an action with an unknown external outcome"
    )
    reconcile.add_argument("action_id")
    _add_runtime_db_option(reconcile)
    reconcile.add_argument(
        "--outcome",
        required=True,
        choices=[outcome.value for outcome in RuntimeReconciliation],
    )
    reconcile.add_argument("--resolved-by", required=True)
    reconcile.add_argument("--reason", required=True)
    reconcile.add_argument(
        "--result-json",
        help="JSON result required when the external effect is proven committed",
    )
    reconcile.set_defaults(handler=_run_runtime_reconciliation)

    audit = commands.add_parser("audit", help="inspect and verify runtime audit receipts")
    _add_runtime_db_option(audit)
    audit.add_argument("--action-id", help="include receipts for one action")
    audit.add_argument("--json", action="store_true", help="write JSON to stdout")
    audit.set_defaults(handler=_run_runtime_audit)

    database = commands.add_parser("database", help="inspect, migrate, and back up runtime state")
    database_commands = database.add_subparsers(dest="database_command", required=True)

    database_status = database_commands.add_parser("status", help="inspect runtime database state")
    _add_runtime_db_option(database_status)
    database_status.add_argument("--json", action="store_true", help="write JSON to stdout")
    database_status.set_defaults(handler=_run_database_status)

    database_migrate = database_commands.add_parser(
        "migrate", help="apply supported runtime schema migrations"
    )
    _add_runtime_db_option(database_migrate)
    database_migrate.set_defaults(handler=_run_database_migrate)

    database_backup = database_commands.add_parser(
        "backup", help="write a consistent runtime database backup"
    )
    _add_runtime_db_option(database_backup)
    database_backup.add_argument("--output", required=True, metavar="PATH")
    database_backup.set_defaults(handler=_run_database_backup)

    mcp = commands.add_parser("mcp", help="run the policy gateway in front of an MCP server")
    mcp_commands = mcp.add_subparsers(dest="mcp_transport", required=True)

    mcp_stdio = mcp_commands.add_parser("stdio", help="serve the MCP gateway over stdio")
    _add_mcp_gateway_options(mcp_stdio)
    mcp_stdio.set_defaults(handler=_run_mcp_gateway)

    mcp_http = mcp_commands.add_parser(
        "http",
        help="serve the MCP gateway over Streamable HTTP",
    )
    _add_mcp_gateway_options(mcp_http)
    mcp_http.add_argument("--host", default="127.0.0.1", help="listen host (default: 127.0.0.1)")
    mcp_http.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    mcp_http.add_argument("--path", default="/mcp", help="MCP endpoint path (default: /mcp)")
    mcp_http.set_defaults(handler=_run_mcp_gateway)

    api = commands.add_parser("api", help="run the authenticated approval HTTP API")
    _add_runtime_db_option(api)
    api.add_argument(
        "--auth-config",
        required=True,
        metavar="PATH",
        help="strict JSON file containing scoped bearer-token SHA-256 values",
    )
    api.add_argument("--host", default="127.0.0.1", help="listen host (default: 127.0.0.1)")
    api.add_argument("--port", type=int, default=8787, help="listen port (default: 8787)")
    api.set_defaults(handler=_run_approval_api)

    auth = commands.add_parser("auth", help="manage approval-service authentication material")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    hash_token = auth_commands.add_parser(
        "hash-token",
        help="hash a bearer token for an approval API auth file",
    )
    hash_token.add_argument(
        "--token-env",
        metavar="NAME",
        help="read the token from this environment variable instead of a hidden prompt",
    )
    hash_token.set_defaults(handler=_run_hash_token)

    webhooks = commands.add_parser("webhooks", help="deliver signed runtime audit webhooks")
    webhook_commands = webhooks.add_subparsers(dest="webhook_command", required=True)
    webhook_run = webhook_commands.add_parser("run", help="run the durable webhook worker")
    _add_runtime_db_option(webhook_run)
    webhook_run.add_argument(
        "--state-db",
        required=True,
        metavar="PATH",
        help="separate durable webhook delivery database",
    )
    webhook_run.add_argument("--config", required=True, metavar="PATH", help="webhook config JSON")
    webhook_run.add_argument("--once", action="store_true", help="process currently due work once")
    webhook_run.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="continuous worker polling interval (default: 1)",
    )
    webhook_run.set_defaults(handler=_run_webhook_worker)

    webhook_status = webhook_commands.add_parser(
        "status",
        help="inspect durable webhook delivery state",
    )
    webhook_status.add_argument("--state-db", required=True, metavar="PATH")
    webhook_status.add_argument("--json", action="store_true", help="write JSON to stdout")
    webhook_status.set_defaults(handler=_run_webhook_status)

    webhook_retry = webhook_commands.add_parser(
        "retry",
        help="requeue one exact dead webhook delivery",
    )
    webhook_retry.add_argument("event_id", help="stable webhook event id")
    webhook_retry.add_argument("--endpoint", required=True, help="configured endpoint id")
    webhook_retry.add_argument("--state-db", required=True, metavar="PATH")
    webhook_retry.set_defaults(handler=_run_webhook_retry)
    return parser


def _add_runtime_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, metavar="PATH", help="runtime SQLite database")


def _add_mcp_gateway_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy", required=True, metavar="PATH", help="runtime policy JSON")
    _add_runtime_db_option(parser)
    parser.add_argument(
        "--namespace",
        default="mcp-gateway",
        help="runtime action namespace (default: mcp-gateway)",
    )
    upstream = parser.add_mutually_exclusive_group(required=True)
    upstream.add_argument("--upstream-url", help="upstream Streamable HTTP MCP endpoint")
    upstream.add_argument("--upstream-command", help="upstream stdio MCP executable")
    parser.add_argument(
        "--upstream-arg",
        action="append",
        default=[],
        metavar="VALUE",
        help="one upstream command argument; repeat as needed",
    )
    parser.add_argument(
        "--upstream-timeout",
        type=float,
        metavar="SECONDS",
        help="optional upstream request timeout",
    )
    parser.add_argument(
        "--idempotency-argument",
        metavar="DOTTED_PATH",
        help=(
            "read stable operation identity from this argument path instead of "
            "params._meta['agentbarrier/idempotencyKey']"
        ),
    )


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[item.scenario_id for item in DEFAULT_SCENARIOS],
        help="run one scenario; repeat to select multiple",
    )
    parser.add_argument("--strict-skips", action="store_true", help="fail when a capability skips")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failure/error")
    parser.add_argument(
        "--approval-profile",
        choices=[profile.value for profile in ApprovalBarrierProfile],
        default=ApprovalBarrierProfile.RUN_WIDE.value,
        help="approval scope for parallel effects (default: run-wide)",
    )
    parser.add_argument("--settle", type=float, default=0.05, help="late-effect window in seconds")
    parser.add_argument("--operation-timeout", type=float, default=5.0)
    parser.add_argument("--tool-timeout", type=float, default=0.05)
    parser.add_argument("--json", metavar="PATH", help="write JSON evidence")
    parser.add_argument("--junit", metavar="PATH", help="write JUnit XML")
    parser.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize terminal output (default: auto)",
    )
    parser.set_defaults(handler=_run_suite)


def _list_scenarios(_: argparse.Namespace) -> int:
    for scenario in DEFAULT_SCENARIOS:
        print(f"{scenario.scenario_id:20} {scenario.capability.value:20} {scenario.name}")
    return 0


def _run_suite(arguments: argparse.Namespace) -> int:
    adapter = (
        ReferenceAdapter()
        if arguments.command == "self-test"
        else _load_adapter(cast(str, arguments.target))
    )
    options = RunnerOptions(
        settle_seconds=arguments.settle,
        operation_timeout_seconds=arguments.operation_timeout,
        tool_timeout_seconds=arguments.tool_timeout,
        strict_skips=arguments.strict_skips,
        scenarios=tuple(arguments.scenario) if arguments.scenario else None,
        fail_fast=arguments.fail_fast,
        approval_profile=ApprovalBarrierProfile(arguments.approval_profile),
    )
    suite = SuiteRunner(options).verify_sync(adapter)
    print(render_console(suite, color=_use_color(arguments.color)))
    if arguments.json:
        write_json(suite, arguments.json)
    if arguments.junit:
        write_junit(suite, arguments.junit)
    if arguments.sarif:
        write_sarif(suite, arguments.sarif)
    return suite.exit_code


def _run_compatibility(arguments: argparse.Namespace) -> int:
    specs = select_adapter_specs(tuple(arguments.adapter) if arguments.adapter else None)
    profiles = (
        tuple(ApprovalBarrierProfile(value) for value in arguments.profile)
        if arguments.profile
        else tuple(ApprovalBarrierProfile)
    )
    evidence = generate_compatibility_evidence(
        specs=specs,
        profiles=profiles,
        settle_seconds=arguments.settle,
        operation_timeout_seconds=arguments.operation_timeout,
        tool_timeout_seconds=arguments.tool_timeout,
    )
    outputs_current = write_compatibility_outputs(
        evidence,
        json_path=arguments.json,
        markdown_path=arguments.markdown,
        check_outputs=arguments.check,
    )
    if arguments.json is None:
        print(dump_compatibility_evidence(evidence), end="")
    if not outputs_current:
        print("compatibility outputs are out of date", file=sys.stderr)
        return 1
    return int(evidence_has_errors(evidence, strict_missing=arguments.strict_missing))


def _run_approvals_list(arguments: argparse.Namespace) -> int:
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        status = RuntimeStatus(arguments.status) if arguments.status else None
        actions = store.list_actions(status=status)
    if arguments.json:
        print(json.dumps([action_payload(action) for action in actions], indent=2, sort_keys=True))
        return 0
    if not actions:
        print("No runtime actions found.")
        return 0
    print(f"{'ACTION ID':36}  {'STATUS':10}  TOOL")
    for action in actions:
        print(f"{action.action_id:36}  {action.status.value:10}  {action.tool_name}")
    return 0


def _run_approvals_show(arguments: argparse.Namespace) -> int:
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        action = store.get_action(cast(str, arguments.action_id))
    payload = action_payload(action)
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            rendered = (
                json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            )
            print(f"{key}: {rendered}")
    return 0


def _run_approval_decision(arguments: argparse.Namespace) -> int:
    decision = cast(RuntimeDecision, arguments.decision)
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        action = store.decide(
            cast(str, arguments.action_id),
            decision,
            decided_by=cast(str, arguments.decided_by),
            reason=cast(str | None, arguments.reason),
        )
    past_tense = "approved" if decision is RuntimeDecision.APPROVE else "rejected"
    print(f"{past_tense} {action.action_id} ({action.tool_name})")
    return 0


def _run_runtime_reconciliation(arguments: argparse.Namespace) -> int:
    outcome = RuntimeReconciliation(cast(str, arguments.outcome))
    raw_result = cast(str | None, arguments.result_json)
    if outcome is RuntimeReconciliation.COMMITTED and raw_result is None:
        raise ValueError("committed reconciliation requires --result-json")
    if outcome is RuntimeReconciliation.NOT_COMMITTED and raw_result is not None:
        raise ValueError("--result-json is valid only for a committed reconciliation")
    result = cast(JsonValue, json.loads(raw_result)) if raw_result is not None else None
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        action = store.reconcile(
            cast(str, arguments.action_id),
            outcome,
            resolved_by=cast(str, arguments.resolved_by),
            reason=cast(str, arguments.reason),
            result=result,
        )
    print(f"reconciled {action.action_id} as {outcome.value} ({action.status.value})")
    return 0


def _run_runtime_audit(arguments: argparse.Namespace) -> int:
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        receipts = store.receipts(action_id=cast(str | None, arguments.action_id))
        chain_valid = store.verify_receipt_chain()
    if arguments.json:
        print(
            json.dumps(
                {
                    "chain_valid": chain_valid,
                    "receipts": [receipt_payload(receipt) for receipt in receipts],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"Receipt chain: {'valid' if chain_valid else 'INVALID'}")
        for receipt in receipts:
            actor = receipt.actor or "-"
            print(
                f"{receipt.sequence:6}  {receipt.event.value:22}  "
                f"{receipt.action_id}  actor={actor}"
            )
    return 0 if chain_valid else 1


def _run_database_status(arguments: argparse.Namespace) -> int:
    database_path = cast(str, arguments.db)
    _require_existing_runtime_db(database_path)
    with SQLiteRuntimeStore(database_path) as store:
        actions = store.list_actions()
        receipts = store.receipts()
        payload = {
            "schema_version": store.schema_version,
            "actions": len(actions),
            "receipts": len(receipts),
            "receipt_chain_valid": store.verify_receipt_chain(),
        }
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Schema version: {payload['schema_version']}")
        print(f"Actions: {payload['actions']}")
        print(f"Receipts: {payload['receipts']}")
        print(f"Receipt chain: {'valid' if payload['receipt_chain_valid'] else 'INVALID'}")
    return 0 if payload["receipt_chain_valid"] else 1


def _run_database_migrate(arguments: argparse.Namespace) -> int:
    with SQLiteRuntimeStore(cast(str, arguments.db)) as store:
        version = store.schema_version
    print(f"Runtime database is at schema version {version}")
    return 0


def _run_database_backup(arguments: argparse.Namespace) -> int:
    database_path = cast(str, arguments.db)
    _require_existing_runtime_db(database_path)
    with SQLiteRuntimeStore(database_path) as store:
        destination = store.backup(cast(str, arguments.output))
    print(f"Runtime database backup written to {destination}")
    return 0


def _run_mcp_gateway(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.mcp.runner import (
            MCPGatewayConfig,
            run_http_gateway,
            run_stdio_gateway,
        )
    except ImportError as error:
        raise ImportError(
            "MCP gateway dependencies are unavailable; install 'agentbarrier[mcp]'"
        ) from error

    config = MCPGatewayConfig(
        policy_path=Path(cast(str, arguments.policy)),
        database_path=Path(cast(str, arguments.db)),
        namespace=cast(str, arguments.namespace),
        upstream_url=cast(str | None, arguments.upstream_url),
        upstream_command=cast(str | None, arguments.upstream_command),
        upstream_args=tuple(cast(list[str], arguments.upstream_arg)),
        upstream_timeout_seconds=cast(float | None, arguments.upstream_timeout),
        idempotency_argument=cast(str | None, arguments.idempotency_argument),
    )
    if arguments.mcp_transport == "stdio":
        run_stdio_gateway(config)
    else:
        run_http_gateway(
            config,
            host=cast(str, arguments.host),
            port=cast(int, arguments.port),
            path=cast(str, arguments.path),
        )
    return 0


def _run_approval_api(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import run_approval_api
    except ImportError as error:
        raise ImportError(
            "approval API dependencies are unavailable; install 'agentbarrier[service]'"
        ) from error
    run_approval_api(
        database_path=cast(str, arguments.db),
        auth_path=cast(str, arguments.auth_config),
        host=cast(str, arguments.host),
        port=cast(int, arguments.port),
    )
    return 0


def _run_hash_token(arguments: argparse.Namespace) -> int:
    from agentbarrier.service.auth import hash_bearer_token

    environment_name = cast(str | None, arguments.token_env)
    if environment_name is not None:
        if not environment_name.strip():
            raise ValueError("--token-env must not be empty")
        token = os.environ.get(environment_name)
        if token is None:
            raise ValueError(f"environment variable {environment_name!r} is not set")
    else:
        token = getpass.getpass("Bearer token: ")
    print(hash_bearer_token(token))
    return 0


def _run_webhook_worker(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import run_webhook_worker
    except ImportError as error:
        raise ImportError(
            "webhook dependencies are unavailable; install 'agentbarrier[service]'"
        ) from error
    counts = run_webhook_worker(
        database_path=cast(str, arguments.db),
        state_path=cast(str, arguments.state_db),
        config_path=cast(str, arguments.config),
        once=cast(bool, arguments.once),
        poll_interval_seconds=cast(float, arguments.poll_interval),
    )
    if counts is not None:
        print(json.dumps(counts, sort_keys=True))
    return 0


def _run_webhook_status(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import webhook_delivery_status
    except ImportError as error:
        raise ImportError(
            "webhook dependencies are unavailable; install 'agentbarrier[service]'"
        ) from error
    snapshots = webhook_delivery_status(cast(str, arguments.state_db))
    payload = [
        {
            "delivery_id": item.delivery_id,
            "endpoint_id": item.endpoint_id,
            "receipt_sequence": item.receipt_sequence,
            "event_id": item.event_id,
            "event_type": item.event_type,
            "status": item.status,
            "attempts": item.attempts,
            "next_attempt_at_ns": item.next_attempt_at_ns,
            "last_status_code": item.last_status_code,
            "last_error": item.last_error,
            "delivered_at_ns": item.delivered_at_ns,
        }
        for item in snapshots
    ]
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not payload:
        print("No webhook deliveries found.")
        return 0
    print(f"{'EVENT ID':24}  {'ENDPOINT':20}  {'STATUS':10}  ATTEMPTS")
    for item in payload:
        print(
            f"{item['event_id']:24}  {item['endpoint_id']:20}  "
            f"{item['status']:10}  {item['attempts']}"
        )
    return 0


def _run_webhook_retry(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import retry_webhook_delivery
    except ImportError as error:
        raise ImportError(
            "webhook dependencies are unavailable; install 'agentbarrier[service]'"
        ) from error
    delivery = retry_webhook_delivery(
        cast(str, arguments.state_db),
        endpoint_id=cast(str, arguments.endpoint),
        event_id=cast(str, arguments.event_id),
    )
    print(f"requeued {delivery.event_id} for endpoint {delivery.endpoint_id}")
    return 0


def _require_existing_runtime_db(path: str) -> None:
    database_path = Path(path).expanduser()
    if not database_path.is_file():
        raise FileNotFoundError(f"runtime database does not exist: {database_path}")


def _use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb" and sys.stdout.isatty()


def _load_adapter(target: str) -> AgentAdapter:
    if ":" not in target:
        raise ValueError("adapter target must use MODULE:ATTRIBUTE syntax")
    module_name, attribute_path = target.split(":", 1)
    if not module_name or not attribute_path:
        raise ValueError("adapter target must include both module and attribute")
    value: Any = importlib.import_module(module_name)
    for segment in attribute_path.split("."):
        value = getattr(value, segment)

    if inspect.isclass(value):
        value = value()
    elif not isinstance(value, AgentAdapter) and callable(value):
        factory = cast(Callable[[], object], value)
        value = factory()
    if not isinstance(value, AgentAdapter):
        raise TypeError(f"{target!r} did not resolve to an AgentAdapter")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and normalize configuration errors to exit status 2."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        AgentBarrierError,
        ImportError,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
