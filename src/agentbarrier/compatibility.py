"""Deterministic compatibility evidence for bundled framework adapters."""

from __future__ import annotations

import importlib
import inspect
import json
import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from agentbarrier import __version__
from agentbarrier.adapter import AgentAdapter
from agentbarrier.models import ApprovalBarrierProfile, ScenarioStatus
from agentbarrier.runner import RunnerOptions, SuiteRunner
from agentbarrier.scenarios import DEFAULT_SCENARIOS

EVIDENCE_SCHEMA_VERSION = "1.0"
EVIDENCE_SCHEMA_URL = (
    "https://raw.githubusercontent.com/binaydhakal/agentbarrier/main/"
    "docs/schemas/compatibility-v1.schema.json"
)
MARKDOWN_START = "<!-- agentbarrier:compatibility:start -->"
MARKDOWN_END = "<!-- agentbarrier:compatibility:end -->"


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """One importable adapter and the distribution that provides its framework."""

    key: str
    label: str
    target: str
    distribution: str
    minimum_python: tuple[int, int] = (3, 10)


DEFAULT_ADAPTER_SPECS: tuple[AdapterSpec, ...] = (
    AdapterSpec(
        key="reference",
        label="Reference",
        target="agentbarrier.adapters.reference:ReferenceAdapter",
        distribution="agentbarrier",
    ),
    AdapterSpec(
        key="openai-agents",
        label="OpenAI Agents Python",
        target="agentbarrier.adapters.openai_agents:OpenAIAgentsAdapter",
        distribution="openai-agents",
    ),
    AdapterSpec(
        key="langgraph",
        label="LangGraph (Python 3.11+)",
        target="agentbarrier.adapters.langgraph:LangGraphAdapter",
        distribution="langgraph",
        minimum_python=(3, 11),
    ),
    AdapterSpec(
        key="pydantic-ai",
        label="PydanticAI",
        target="agentbarrier.adapters.pydantic_ai:PydanticAIAdapter",
        distribution="pydantic-ai-slim",
    ),
    AdapterSpec(
        key="google-adk",
        label="Google ADK",
        target="agentbarrier.adapters.google_adk:GoogleADKAdapter",
        distribution="google-adk",
    ),
    AdapterSpec(
        key="autogen",
        label="AutoGen Core (single-threaded runtime)",
        target="agentbarrier.adapters.autogen:AutoGenAdapter",
        distribution="autogen-core",
    ),
)


def select_adapter_specs(keys: Sequence[str] | None) -> tuple[AdapterSpec, ...]:
    """Select adapter specifications in canonical order."""

    if keys is None:
        return DEFAULT_ADAPTER_SPECS
    requested = set(keys)
    known = {spec.key for spec in DEFAULT_ADAPTER_SPECS}
    unknown = requested - known
    if unknown:
        raise ValueError(f"unknown compatibility adapters: {', '.join(sorted(unknown))}")
    return tuple(spec for spec in DEFAULT_ADAPTER_SPECS if spec.key in requested)


def generate_compatibility_evidence(
    *,
    specs: Sequence[AdapterSpec] = DEFAULT_ADAPTER_SPECS,
    profiles: Sequence[ApprovalBarrierProfile] = tuple(ApprovalBarrierProfile),
    settle_seconds: float = 0.01,
    operation_timeout_seconds: float = 5.0,
    tool_timeout_seconds: float = 0.05,
) -> dict[str, Any]:
    """Run bundled probes and return stable evidence without timing or run identifiers."""

    selected_profiles = tuple(profiles)
    if not selected_profiles:
        raise ValueError("at least one approval profile is required")
    if len(set(selected_profiles)) != len(selected_profiles):
        raise ValueError("approval profiles must not be duplicated")
    adapters = [
        _generate_adapter_evidence(
            spec,
            profiles=selected_profiles,
            settle_seconds=settle_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
        )
        for spec in specs
    ]
    return {
        "$schema": EVIDENCE_SCHEMA_URL,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generator": {
            "name": "agentbarrier",
            "version": __version__,
            "python_implementation": platform.python_implementation(),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "profiles": [profile.value for profile in selected_profiles],
        "adapters": adapters,
    }


def _generate_adapter_evidence(
    spec: AdapterSpec,
    *,
    profiles: tuple[ApprovalBarrierProfile, ...],
    settle_seconds: float,
    operation_timeout_seconds: float,
    tool_timeout_seconds: float,
) -> dict[str, Any]:
    minimum = f">={spec.minimum_python[0]}.{spec.minimum_python[1]}"
    base: dict[str, Any] = {
        "key": spec.key,
        "label": spec.label,
        "target": spec.target,
        "python_requirement": minimum,
        "distribution": {"name": spec.distribution, "version": None},
        "available": False,
        "unavailable_reason": None,
        "adapter_name": None,
        "capabilities": [],
        "profiles": {},
    }
    if sys.version_info[:2] < spec.minimum_python:
        base["unavailable_reason"] = f"requires Python {minimum}"
        return base
    try:
        distribution_version = version(spec.distribution)
    except PackageNotFoundError:
        base["unavailable_reason"] = f"distribution {spec.distribution!r} is not installed"
        return base
    base["distribution"]["version"] = distribution_version
    try:
        initial_adapter = _load_adapter(spec.target)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        base["unavailable_reason"] = f"{type(exc).__name__} while loading {spec.target}"
        return base

    base["available"] = True
    base["adapter_name"] = initial_adapter.name
    base["capabilities"] = sorted(capability.value for capability in initial_adapter.capabilities)
    profile_evidence: dict[str, Any] = {}
    for profile in profiles:
        adapter = _load_adapter(spec.target)
        suite = SuiteRunner(
            RunnerOptions(
                settle_seconds=settle_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
                tool_timeout_seconds=tool_timeout_seconds,
                approval_profile=profile,
            )
        ).verify_sync(adapter)
        profile_evidence[profile.value] = {
            "passed": suite.passed,
            "summary": {
                "passed": suite.passed_count,
                "failed": suite.failed_count,
                "errors": suite.error_count,
                "skipped": suite.skipped_count,
            },
            "results": [
                {
                    "scenario": result.scenario_id,
                    "capability": _SCENARIO_CAPABILITIES[result.scenario_id],
                    "status": result.status.value,
                    "finding_code": result.finding.code if result.finding else None,
                    "finding_title": result.finding.title if result.finding else None,
                    "detail": (
                        result.detail
                        if result.status in {ScenarioStatus.ERROR, ScenarioStatus.SKIPPED}
                        else None
                    ),
                }
                for result in suite.results
            ],
        }
    base["profiles"] = profile_evidence
    return base


_SCENARIO_CAPABILITIES = {
    scenario.scenario_id: scenario.capability.value for scenario in DEFAULT_SCENARIOS
}


def _load_adapter(target: str) -> AgentAdapter:
    module_name, separator, attribute_path = target.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ValueError("adapter target must use MODULE:ATTRIBUTE syntax")
    value: object = importlib.import_module(module_name)
    for segment in attribute_path.split("."):
        value = getattr(value, segment)
    if inspect.isclass(value) or (not isinstance(value, AgentAdapter) and callable(value)):
        value = value()
    if not isinstance(value, AgentAdapter):
        raise TypeError(f"{target!r} did not resolve to an AgentAdapter")
    return value


def evidence_has_errors(evidence: Mapping[str, Any], *, strict_missing: bool) -> bool:
    """Return whether generation found runtime errors or disallowed missing adapters."""

    adapters = cast(list[dict[str, Any]], evidence["adapters"])
    for adapter in adapters:
        if not adapter["available"]:
            if strict_missing:
                return True
            continue
        profiles = cast(dict[str, dict[str, Any]], adapter["profiles"])
        for profile in profiles.values():
            results = cast(list[dict[str, Any]], profile["results"])
            if any(result["status"] == ScenarioStatus.ERROR.value for result in results):
                return True
    return False


def dump_compatibility_evidence(evidence: Mapping[str, Any]) -> str:
    """Serialize evidence with stable ordering and a trailing newline."""

    return json.dumps(evidence, indent=2, sort_keys=True) + "\n"


def render_compatibility_section(evidence: Mapping[str, Any]) -> str:
    """Render the canonical run-wide compatibility table from evidence."""

    generator = cast(dict[str, str], evidence["generator"])
    adapters = cast(list[dict[str, Any]], evidence["adapters"])
    scenario_ids = [scenario.scenario_id for scenario in DEFAULT_SCENARIOS]
    headings = [
        "Adapter",
        "Version",
        "Approval",
        "Rejection",
        "Args",
        "Replay",
        "Unknown",
        "Cancel",
        "Timeout",
        "Parallel",
        "Delegation",
        "Audit",
    ]
    lines = [
        MARKDOWN_START,
        (
            f"Canonical evidence: Python {generator['python_version']} · "
            f"AgentBarrier {generator['version']} · `run-wide` profile"
        ),
        "",
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
    ]
    for adapter in adapters:
        distribution = cast(dict[str, str | None], adapter["distribution"])
        if not adapter["available"]:
            cells = [adapter["label"], distribution["version"] or "Not installed"]
            cells.extend("Unavailable" for _ in scenario_ids)
        else:
            profiles = cast(dict[str, dict[str, Any]], adapter["profiles"])
            run_wide = profiles.get(ApprovalBarrierProfile.RUN_WIDE.value)
            if run_wide is None:
                raise ValueError("run-wide profile evidence is required for Markdown rendering")
            results = {
                result["scenario"]: result
                for result in cast(list[dict[str, Any]], run_wide["results"])
            }
            cells = [adapter["label"], cast(str, distribution["version"])]
            cells.extend(_markdown_status(results[scenario_id]) for scenario_id in scenario_ids)
        lines.append("| " + " | ".join(cast(list[str], cells)) + " |")
    lines.extend([MARKDOWN_END])
    return "\n".join(lines)


def _markdown_status(result: Mapping[str, Any]) -> str:
    status = result["status"]
    if status == ScenarioStatus.PASSED.value:
        return "Pass"
    if status == ScenarioStatus.SKIPPED.value:
        return "—"
    if status == ScenarioStatus.FAILED.value:
        return f"**{result['finding_code'] or 'Fail'}**"
    return "**Error**"


def replace_compatibility_section(document: str, evidence: Mapping[str, Any]) -> str:
    """Replace the generated section and reject missing or duplicated markers."""

    if document.count(MARKDOWN_START) != 1 or document.count(MARKDOWN_END) != 1:
        raise ValueError("compatibility document must contain one generated-section marker pair")
    start = document.index(MARKDOWN_START)
    end = document.index(MARKDOWN_END, start) + len(MARKDOWN_END)
    return document[:start] + render_compatibility_section(evidence) + document[end:]


def write_compatibility_outputs(
    evidence: Mapping[str, Any],
    *,
    json_path: str | Path | None,
    markdown_path: str | Path | None,
    check_outputs: bool,
) -> bool:
    """Write outputs, or check that every selected output is current."""

    if check_outputs and json_path is None and markdown_path is None:
        raise ValueError("--check requires --json or --markdown")
    outputs_current = True
    if json_path is not None:
        target = Path(json_path)
        rendered = dump_compatibility_evidence(evidence)
        if check_outputs:
            outputs_current = target.is_file() and target.read_text(encoding="utf-8") == rendered
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
    if markdown_path is None:
        return outputs_current
    target = Path(markdown_path)
    if check_outputs and not target.is_file():
        return False
    original = target.read_text(encoding="utf-8")
    updated = replace_compatibility_section(original, evidence)
    if check_outputs:
        return outputs_current and original == updated
    target.write_text(updated, encoding="utf-8")
    return True
