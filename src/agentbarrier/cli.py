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
from contextlib import AbstractContextManager
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
from agentbarrier.runtime.factory import open_runtime_store
from agentbarrier.runtime.models import RuntimeReconciliation, RuntimeStatus
from agentbarrier.runtime.protocol import RuntimeStore
from agentbarrier.runtime.serialization import (
    action_payload,
    control_receipt_payload,
    limit_payload,
    limit_usage_payload,
    pause_payload,
    receipt_payload,
)
from agentbarrier.runtime.store import SQLiteRuntimeStore
from agentbarrier.scenarios import DEFAULT_SCENARIOS


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""

    parser = argparse.ArgumentParser(
        prog="agentbarrier",
        description="Enforce and verify transaction safety for AI-agent tools.",
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

    controls = commands.add_parser(
        "controls",
        help="manage emergency pauses and atomic execution limits",
    )
    control_commands = controls.add_subparsers(dest="control_command", required=True)

    control_status = control_commands.add_parser(
        "status",
        help="show active pauses, limits, usage, and control-audit integrity",
    )
    _add_runtime_db_option(control_status)
    control_status.add_argument("--json", action="store_true", help="write JSON to stdout")
    control_status.set_defaults(handler=_run_controls_status)

    pause = control_commands.add_parser("pause", help="activate an emergency pause")
    _add_runtime_db_option(pause)
    _add_control_scope_options(pause)
    pause.add_argument("--paused-by", required=True, help="operator identity")
    pause.add_argument("--reason", required=True, help="incident or change reason")
    pause.set_defaults(handler=_run_controls_pause)

    resume = control_commands.add_parser("resume", help="clear one exact emergency pause")
    _add_runtime_db_option(resume)
    _add_control_scope_options(resume)
    resume.add_argument("--resumed-by", required=True, help="operator identity")
    resume.add_argument("--reason", required=True, help="recovery or change reason")
    resume.set_defaults(handler=_run_controls_resume)

    limit_set = control_commands.add_parser(
        "limit-set",
        help="create or update a fixed-window execution limit",
    )
    limit_set.add_argument("limit_id")
    _add_runtime_db_option(limit_set)
    _add_control_scope_options(limit_set)
    limit_set.add_argument("--window-seconds", type=float, required=True)
    limit_set.add_argument("--max-actions", type=int)
    limit_set.add_argument(
        "--value-argument",
        help="dot-separated non-negative integer argument, such as amount_cents",
    )
    limit_set.add_argument("--max-value", type=int)
    limit_set.add_argument("--updated-by", required=True, help="operator identity")
    limit_set.add_argument("--reason", required=True, help="risk-control reason")
    limit_set.set_defaults(handler=_run_controls_limit_set)

    limit_disable = control_commands.add_parser(
        "limit-disable",
        help="disable a limit without deleting its usage history",
    )
    limit_disable.add_argument("limit_id")
    _add_runtime_db_option(limit_disable)
    limit_disable.add_argument("--updated-by", required=True, help="operator identity")
    limit_disable.add_argument("--reason", required=True, help="change reason")
    limit_disable.set_defaults(handler=_run_controls_limit_disable)

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
    database_migrate.add_argument(
        "--postgres-create-schema",
        action="store_true",
        help="create the PostgreSQL schema during this migration",
    )
    database_migrate.set_defaults(handler=_run_database_migrate)

    database_backup = database_commands.add_parser(
        "backup", help="write a consistent runtime database backup"
    )
    database_backup.add_argument(
        "--db",
        required=True,
        metavar="PATH",
        help="runtime SQLite database; PostgreSQL deployments use pg_dump",
    )
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
    mcp_http.add_argument(
        "--auth-config",
        metavar="PATH",
        help="scoped bearer-token digest file; required for non-loopback listeners",
    )
    mcp_http.add_argument(
        "--max-request-bytes",
        type=int,
        default=1024 * 1024,
        metavar="BYTES",
        help="maximum MCP request body size (default: 1048576)",
    )
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

    dashboard = commands.add_parser(
        "dashboard",
        help="run the server-rendered approval dashboard",
    )
    _add_runtime_db_option(dashboard)
    dashboard.add_argument(
        "--auth-config",
        required=True,
        metavar="PATH",
        help="strict scoped bearer-token digest file used for reviewer sign-in",
    )
    dashboard.add_argument("--host", default="127.0.0.1", help="listen host (default: 127.0.0.1)")
    dashboard.add_argument("--port", type=int, default=8788, help="listen port (default: 8788)")
    dashboard.add_argument(
        "--public-origin",
        help="external HTTPS origin used for same-origin validation",
    )
    dashboard.add_argument(
        "--cookie-secure",
        action="store_true",
        help="send session cookies only over HTTPS (required off loopback)",
    )
    dashboard.add_argument(
        "--session-ttl",
        type=float,
        default=8 * 60 * 60,
        metavar="SECONDS",
        help="browser session lifetime (default: 28800)",
    )
    dashboard.set_defaults(handler=_run_approval_dashboard)

    auth = commands.add_parser("auth", help="manage AgentBarrier service authentication material")
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

    slack = commands.add_parser("slack", help="deliver and decide approvals in Slack")
    slack_commands = slack.add_subparsers(dest="slack_command", required=True)

    slack_serve = slack_commands.add_parser(
        "serve",
        help="run signed Slack interactions and the durable notification worker",
    )
    _add_runtime_db_option(slack_serve)
    slack_serve.add_argument(
        "--state-db",
        required=True,
        metavar="PATH",
        help="separate durable Slack notification database",
    )
    slack_serve.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="strict Slack workspace, reviewer, and secret environment config",
    )
    slack_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="listen host (default: 127.0.0.1)",
    )
    slack_serve.add_argument("--port", type=int, default=8789, help="listen port (default: 8789)")
    slack_serve.add_argument(
        "--interaction-path",
        default="/slack/interactions",
        help="signed Slack endpoint path (default: /slack/interactions)",
    )
    slack_serve.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="notification polling interval (default: 1)",
    )
    slack_serve.set_defaults(handler=_run_slack_service)

    slack_status = slack_commands.add_parser(
        "status",
        help="inspect durable Slack notification state",
    )
    slack_status.add_argument("--state-db", required=True, metavar="PATH")
    slack_status.add_argument("--json", action="store_true", help="write JSON to stdout")
    slack_status.set_defaults(handler=_run_slack_status)

    slack_retry = slack_commands.add_parser(
        "retry",
        help="requeue one exact dead Slack notification",
    )
    slack_retry.add_argument("action_id", help="runtime action id")
    slack_retry.add_argument("--state-db", required=True, metavar="PATH")
    slack_retry.set_defaults(handler=_run_slack_retry)
    return parser


def _add_runtime_db_option(parser: argparse.ArgumentParser) -> None:
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--db", metavar="PATH", help="runtime SQLite database")
    backend.add_argument(
        "--postgres-dsn-env",
        metavar="NAME",
        help="environment variable containing the PostgreSQL DSN",
    )
    parser.add_argument(
        "--postgres-schema",
        default="agentbarrier",
        metavar="NAME",
        help="dedicated PostgreSQL schema (default: agentbarrier)",
    )


def _open_cli_runtime_store(
    arguments: argparse.Namespace,
) -> AbstractContextManager[RuntimeStore]:
    return open_runtime_store(
        database_path=cast(str | None, arguments.db),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
        postgres_create_schema=bool(getattr(arguments, "postgres_create_schema", False)),
        postgres_migrate=getattr(arguments, "database_command", None) == "migrate",
    )


def _require_existing_selected_store(arguments: argparse.Namespace) -> None:
    database_path = cast(str | None, arguments.db)
    if database_path is not None:
        _require_existing_runtime_db(database_path)


def _add_control_scope_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", help="limit the control to one service namespace")
    parser.add_argument("--tool", dest="tool_name", help="limit the control to one tool name")


def _add_mcp_gateway_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy", required=True, metavar="PATH", help="runtime policy JSON")
    _add_runtime_db_option(parser)
    parser.add_argument(
        "--namespace",
        default="mcp-gateway",
        help="runtime action namespace (default: mcp-gateway)",
    )
    parser.add_argument(
        "--organization",
        default="default",
        help="organization recorded on every action (default: default)",
    )
    parser.add_argument(
        "--requested-by",
        help="service identity requesting actions; required with a non-default organization",
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
        "--upstream-bearer-token-env",
        metavar="NAME",
        help="read an upstream HTTP bearer token from this environment variable",
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
    with _open_cli_runtime_store(arguments) as store:
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
    with _open_cli_runtime_store(arguments) as store:
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
    with _open_cli_runtime_store(arguments) as store:
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
    with _open_cli_runtime_store(arguments) as store:
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
    with _open_cli_runtime_store(arguments) as store:
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


def _run_controls_status(arguments: argparse.Namespace) -> int:
    _require_existing_selected_store(arguments)
    with _open_cli_runtime_store(arguments) as store:
        pauses = store.list_pauses()
        limits = store.list_limits()
        usage = store.limit_usage()
        receipts = store.control_receipts()
        chain_valid = store.verify_control_receipt_chain()
    payload = {
        "control_chain_valid": chain_valid,
        "control_receipts": [control_receipt_payload(receipt) for receipt in receipts],
        "limits": [limit_payload(limit) for limit in limits],
        "pauses": [pause_payload(pause) for pause in pauses],
        "usage": [limit_usage_payload(item) for item in usage],
    }
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Control receipt chain: {'valid' if chain_valid else 'INVALID'}")
        print(f"Active pauses: {len(pauses)}")
        for pause in pauses:
            namespace = pause.namespace or "*"
            tool_name = pause.tool_name or "*"
            print(
                f"  namespace={namespace} tool={tool_name} "
                f"by={pause.paused_by} reason={pause.reason}"
            )
        print(f"Limits: {len(limits)}")
        usage_by_id = {item.limit_id: item for item in usage}
        for limit in limits:
            current = usage_by_id[limit.limit_id]
            state = "enabled" if limit.enabled else "disabled"
            print(
                f"  {limit.limit_id} {state} actions={current.actions_used} "
                f"value={current.value_used}"
            )
    return 0 if chain_valid else 1


def _run_controls_pause(arguments: argparse.Namespace) -> int:
    with _open_cli_runtime_store(arguments) as store:
        pause = store.set_pause(
            namespace=cast(str | None, arguments.namespace),
            tool_name=cast(str | None, arguments.tool_name),
            paused_by=cast(str, arguments.paused_by),
            reason=cast(str, arguments.reason),
        )
    namespace = pause.namespace or "*"
    tool_name = pause.tool_name or "*"
    print(f"paused namespace={namespace} tool={tool_name}")
    return 0


def _run_controls_resume(arguments: argparse.Namespace) -> int:
    namespace = cast(str | None, arguments.namespace)
    tool_name = cast(str | None, arguments.tool_name)
    _require_existing_selected_store(arguments)
    with _open_cli_runtime_store(arguments) as store:
        cleared = store.clear_pause(
            namespace=namespace,
            tool_name=tool_name,
            resumed_by=cast(str, arguments.resumed_by),
            reason=cast(str, arguments.reason),
        )
    rendered_namespace = namespace or "*"
    rendered_tool = tool_name or "*"
    if cleared:
        print(f"resumed namespace={rendered_namespace} tool={rendered_tool}")
    else:
        print(f"no active pause for namespace={rendered_namespace} tool={rendered_tool}")
    return 0


def _run_controls_limit_set(arguments: argparse.Namespace) -> int:
    with _open_cli_runtime_store(arguments) as store:
        limit = store.configure_limit(
            cast(str, arguments.limit_id),
            namespace=cast(str | None, arguments.namespace),
            tool_name=cast(str | None, arguments.tool_name),
            window_seconds=cast(float, arguments.window_seconds),
            max_actions=cast(int | None, arguments.max_actions),
            value_argument=cast(str | None, arguments.value_argument),
            max_value=cast(int | None, arguments.max_value),
            updated_by=cast(str, arguments.updated_by),
            reason=cast(str, arguments.reason),
        )
    print(f"configured limit {limit.limit_id}")
    return 0


def _run_controls_limit_disable(arguments: argparse.Namespace) -> int:
    _require_existing_selected_store(arguments)
    with _open_cli_runtime_store(arguments) as store:
        limit = store.disable_limit(
            cast(str, arguments.limit_id),
            updated_by=cast(str, arguments.updated_by),
            reason=cast(str, arguments.reason),
        )
    print(f"disabled limit {limit.limit_id}")
    return 0


def _run_database_status(arguments: argparse.Namespace) -> int:
    _require_existing_selected_store(arguments)
    with _open_cli_runtime_store(arguments) as store:
        actions = store.list_actions()
        receipts = store.receipts()
        control_receipts = store.control_receipts()
        payload = {
            "schema_version": store.schema_version,
            "actions": len(actions),
            "receipts": len(receipts),
            "receipt_chain_valid": store.verify_receipt_chain(),
            "active_pauses": len(store.list_pauses()),
            "limits": len(store.list_limits()),
            "control_receipts": len(control_receipts),
            "control_receipt_chain_valid": store.verify_control_receipt_chain(),
        }
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Schema version: {payload['schema_version']}")
        print(f"Actions: {payload['actions']}")
        print(f"Receipts: {payload['receipts']}")
        print(f"Receipt chain: {'valid' if payload['receipt_chain_valid'] else 'INVALID'}")
        print(f"Active pauses: {payload['active_pauses']}")
        print(f"Limits: {payload['limits']}")
        print(f"Control receipts: {payload['control_receipts']}")
        print(
            "Control receipt chain: "
            f"{'valid' if payload['control_receipt_chain_valid'] else 'INVALID'}"
        )
    return 0 if payload["receipt_chain_valid"] and payload["control_receipt_chain_valid"] else 1


def _run_database_migrate(arguments: argparse.Namespace) -> int:
    with _open_cli_runtime_store(arguments) as store:
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
        database_path=(Path(cast(str, arguments.db)) if arguments.db is not None else None),
        namespace=cast(str, arguments.namespace),
        organization_id=cast(str, arguments.organization),
        requested_by=cast(str | None, arguments.requested_by),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
        upstream_url=cast(str | None, arguments.upstream_url),
        upstream_command=cast(str | None, arguments.upstream_command),
        upstream_args=tuple(cast(list[str], arguments.upstream_arg)),
        upstream_timeout_seconds=cast(float | None, arguments.upstream_timeout),
        upstream_bearer_token_env=cast(str | None, arguments.upstream_bearer_token_env),
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
            auth_path=cast(str | None, arguments.auth_config),
            max_request_body_size=cast(int, arguments.max_request_bytes),
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
        database_path=cast(str | None, arguments.db),
        auth_path=cast(str, arguments.auth_config),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
        host=cast(str, arguments.host),
        port=cast(int, arguments.port),
    )
    return 0


def _run_approval_dashboard(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import run_approval_dashboard
    except ImportError as error:
        raise ImportError(
            "dashboard dependencies are unavailable; install 'agentbarrier[service]'"
        ) from error
    run_approval_dashboard(
        database_path=cast(str | None, arguments.db),
        auth_path=cast(str, arguments.auth_config),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
        host=cast(str, arguments.host),
        port=cast(int, arguments.port),
        public_origin=cast(str | None, arguments.public_origin),
        cookie_secure=cast(bool, arguments.cookie_secure),
        session_ttl_seconds=cast(float, arguments.session_ttl),
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
        database_path=cast(str | None, arguments.db),
        state_path=cast(str, arguments.state_db),
        config_path=cast(str, arguments.config),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
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


def _run_slack_service(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import run_slack_service
    except ImportError as error:
        raise ImportError(
            "Slack dependencies are unavailable; install 'agentbarrier[slack]'"
        ) from error
    run_slack_service(
        database_path=cast(str | None, arguments.db),
        state_path=cast(str, arguments.state_db),
        config_path=cast(str, arguments.config),
        postgres_dsn_env=cast(str | None, arguments.postgres_dsn_env),
        postgres_schema=cast(str, arguments.postgres_schema),
        host=cast(str, arguments.host),
        port=cast(int, arguments.port),
        interaction_path=cast(str, arguments.interaction_path),
        poll_interval_seconds=cast(float, arguments.poll_interval),
    )
    return 0


def _run_slack_status(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import slack_notification_status
    except ImportError as error:
        raise ImportError(
            "Slack dependencies are unavailable; install 'agentbarrier[slack]'"
        ) from error
    snapshots = slack_notification_status(cast(str, arguments.state_db))
    payload = [
        {
            "action_id": item.action_id,
            "request_digest": item.request_digest,
            "channel_id": item.channel_id,
            "status": item.status,
            "attempts": item.attempts,
            "next_attempt_at_ns": item.next_attempt_at_ns,
            "message_ts": item.message_ts,
            "last_status_code": item.last_status_code,
            "last_error": item.last_error,
            "decided_by": item.decided_by,
            "decision": item.decision,
        }
        for item in snapshots
    ]
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if not payload:
        print("No Slack notifications found.")
        return 0
    print(f"{'ACTION ID':36}  {'STATUS':10}  ATTEMPTS  MESSAGE TS")
    for item in payload:
        print(
            f"{item['action_id']:36}  {item['status']:10}  "
            f"{item['attempts']:8}  {item['message_ts'] or '-'}"
        )
    return 0


def _run_slack_retry(arguments: argparse.Namespace) -> int:
    try:
        from agentbarrier.service.runner import retry_slack_notification
    except ImportError as error:
        raise ImportError(
            "Slack dependencies are unavailable; install 'agentbarrier[slack]'"
        ) from error
    notification = retry_slack_notification(
        cast(str, arguments.state_db),
        action_id=cast(str, arguments.action_id),
    )
    print(f"requeued Slack notification for action {notification.action_id}")
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
