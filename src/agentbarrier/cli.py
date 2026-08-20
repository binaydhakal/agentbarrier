"""Command-line interface."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
from collections.abc import Callable, Sequence
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
from agentbarrier.runtime.models import (
    RuntimeAction,
    RuntimeReceipt,
    RuntimeReconciliation,
    RuntimeStatus,
)
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
    return parser


def _add_runtime_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, metavar="PATH", help="runtime SQLite database")


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
        print(json.dumps([_action_payload(action) for action in actions], indent=2, sort_keys=True))
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
    payload = _action_payload(action)
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
                    "receipts": [_receipt_payload(receipt) for receipt in receipts],
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


def _action_payload(action: RuntimeAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "namespace": action.namespace,
        "tool_name": action.tool_name,
        "arguments": dict(action.arguments),
        "idempotency_key": action.idempotency_key,
        "request_digest": action.request_digest,
        "policy_version": action.policy_version,
        "policy_rule": action.policy_rule,
        "policy_effect": action.policy_effect.value,
        "status": action.status.value,
        "created_at_ns": action.created_at_ns,
        "updated_at_ns": action.updated_at_ns,
        "expires_at_ns": action.expires_at_ns,
        "execution_lease_expires_at_ns": action.execution_lease_expires_at_ns,
        "result": action.result if action.result_available else None,
        "result_available": action.result_available,
        "error": action.error,
        "decided_by": action.decided_by,
        "decision_reason": action.decision_reason,
    }


def _receipt_payload(receipt: RuntimeReceipt) -> dict[str, object]:
    return {
        "sequence": receipt.sequence,
        "action_id": receipt.action_id,
        "event": receipt.event.value,
        "timestamp_ns": receipt.timestamp_ns,
        "request_digest": receipt.request_digest,
        "actor": receipt.actor,
        "detail": receipt.detail,
        "previous_hash": receipt.previous_hash,
        "receipt_hash": receipt.receipt_hash,
    }


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
    except (AgentBarrierError, ImportError, AttributeError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
