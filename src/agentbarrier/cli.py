"""Command-line interface."""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, cast

from agentbarrier import __version__
from agentbarrier.adapter import AgentAdapter
from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.compatibility import (
    DEFAULT_ADAPTER_SPECS,
    dump_compatibility_evidence,
    evidence_has_errors,
    generate_compatibility_evidence,
    select_adapter_specs,
    write_compatibility_outputs,
)
from agentbarrier.models import ApprovalBarrierProfile
from agentbarrier.reporters import render_console, write_json, write_junit, write_sarif
from agentbarrier.runner import RunnerOptions, SuiteRunner
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
        choices=[spec.key for spec in DEFAULT_ADAPTER_SPECS],
        help="include one bundled adapter; repeat to select multiple (default: all)",
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
    return parser


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
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
