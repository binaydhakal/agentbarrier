"""Console, JSON, JUnit, and SARIF report generation."""

from __future__ import annotations

import json

# ElementTree is used only for generation; this module never parses XML input.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from typing import Any

from agentbarrier.models import (
    AuditReceipt,
    EffectEvent,
    Finding,
    ScenarioResult,
    ScenarioStatus,
    SuiteResult,
)


def render_console(suite: SuiteResult, *, color: bool = False) -> str:
    """Render a compact terminal report without optional dependencies."""

    lines = [f"AgentBarrier · {suite.adapter}"]
    labels = {
        ScenarioStatus.PASSED: "PASS",
        ScenarioStatus.FAILED: "FAIL",
        ScenarioStatus.SKIPPED: "SKIP",
        ScenarioStatus.ERROR: "ERROR",
    }
    colors = {
        ScenarioStatus.PASSED: "32;1",
        ScenarioStatus.FAILED: "31;1",
        ScenarioStatus.SKIPPED: "33;1",
        ScenarioStatus.ERROR: "31;1",
    }
    for result in suite.results:
        label = _paint(f"{labels[result.status]:5}", colors[result.status], enabled=color)
        line = f"[{label}] {result.scenario_id} ({result.duration_seconds:.3f}s)"
        lines.append(line)
        if result.finding is not None:
            code = _paint(result.finding.code, "31;1", enabled=color)
            lines.append(f"        {code}: {result.finding.title}")
            lines.append(f"        Expected: {result.finding.expected}")
            lines.append(f"        Observed: {result.finding.observed}")
            lines.append(f"        Fix: {result.finding.remediation}")
        elif result.detail:
            lines.append(f"        {result.detail}")
    summary = (
        "Summary: "
        f"{suite.passed_count} passed, {suite.failed_count} failed, "
        f"{suite.error_count} errors, {suite.skipped_count} skipped"
    )
    lines.append(_paint(summary, "32;1" if suite.passed else "31;1", enabled=color))
    return "\n".join(lines)


def _paint(value: str, code: str, *, enabled: bool) -> str:
    if not enabled:
        return value
    return f"\x1b[{code}m{value}\x1b[0m"


def suite_to_dict(suite: SuiteResult) -> dict[str, Any]:
    """Convert a suite to a stable JSON-compatible schema."""

    return {
        "schema_version": "1.0",
        "adapter": suite.adapter,
        "passed": suite.passed,
        "strict_skips": suite.strict_skips,
        "summary": {
            "passed": suite.passed_count,
            "failed": suite.failed_count,
            "errors": suite.error_count,
            "skipped": suite.skipped_count,
        },
        "results": [_result_to_dict(result) for result in suite.results],
    }


def write_json(suite: SuiteResult, path: str | Path) -> None:
    """Write the stable JSON report."""

    _write(path, json.dumps(suite_to_dict(suite), indent=2, sort_keys=True) + "\n")


def write_junit(suite: SuiteResult, path: str | Path) -> None:
    """Write JUnit XML understood by common CI systems."""

    testsuite = ET.Element(
        "testsuite",
        {
            "name": f"agentbarrier.{suite.adapter}",
            "tests": str(len(suite.results)),
            "failures": str(suite.failed_count),
            "errors": str(suite.error_count),
            "skipped": str(suite.skipped_count),
            "time": f"{sum(item.duration_seconds for item in suite.results):.6f}",
        },
    )
    for result in suite.results:
        case = ET.SubElement(
            testsuite,
            "testcase",
            {
                "classname": f"agentbarrier.{suite.adapter}",
                "name": result.scenario_id,
                "time": f"{result.duration_seconds:.6f}",
            },
        )
        if result.status is ScenarioStatus.FAILED:
            node = ET.SubElement(case, "failure", {"message": _message(result)})
            node.text = _details(result)
        elif result.status is ScenarioStatus.ERROR:
            node = ET.SubElement(case, "error", {"message": _message(result)})
            node.text = result.detail
        elif result.status is ScenarioStatus.SKIPPED:
            ET.SubElement(case, "skipped", {"message": result.detail or "unsupported"})
        output = ET.SubElement(case, "system-out")
        output.text = json.dumps(
            {
                "effect_events": [_event_to_dict(event) for event in result.events],
                "audit_receipts": [_receipt_to_dict(receipt) for receipt in result.receipts],
            },
            sort_keys=True,
        )
    root = ET.Element("testsuites")
    root.append(testsuite)
    ET.indent(root)
    _write(path, ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n")


def write_sarif(suite: SuiteResult, path: str | Path) -> None:
    """Write SARIF 2.1.0 findings for code-scanning interfaces."""

    failed = [result for result in suite.results if result.finding is not None]
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for result in failed:
        finding = result.finding
        if finding is None:  # pragma: no cover - `failed` filters this out
            continue
        rules[finding.code] = {
            "id": finding.code,
            "name": result.scenario_id,
            "shortDescription": {"text": finding.title},
            "fullDescription": {"text": finding.expected},
            "help": {"text": finding.remediation},
            "defaultConfiguration": {"level": "error"},
        }
        results.append(
            {
                "ruleId": finding.code,
                "level": "error",
                "message": {"text": finding.observed},
                "properties": {
                    "adapter": suite.adapter,
                    "scenario": result.scenario_id,
                    "effectEvents": [_event_to_dict(event) for event in result.events],
                    "auditReceipts": [_receipt_to_dict(receipt) for receipt in result.receipts],
                },
            }
        )
    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "AgentBarrier",
                        "informationUri": "https://github.com/binaydhakal/agentbarrier",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }
    _write(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _result_to_dict(result: ScenarioResult) -> dict[str, Any]:
    return {
        "id": result.scenario_id,
        "name": result.name,
        "status": result.status.value,
        "duration_seconds": result.duration_seconds,
        "detail": result.detail,
        "finding": _finding_to_dict(result.finding) if result.finding else None,
        "events": [_event_to_dict(event) for event in result.events],
        "receipts": [_receipt_to_dict(receipt) for receipt in result.receipts],
    }


def _finding_to_dict(finding: Finding) -> dict[str, str]:
    return {
        "code": finding.code,
        "title": finding.title,
        "expected": finding.expected,
        "observed": finding.observed,
        "remediation": finding.remediation,
    }


def _event_to_dict(event: EffectEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "run_id": event.run_id,
        "action_id": event.action_id,
        "tool_name": event.tool_name,
        "phase": event.phase.value,
        "arguments": dict(event.arguments),
        "timestamp_ns": event.timestamp_ns,
        "detail": event.detail,
    }


def _receipt_to_dict(receipt: AuditReceipt) -> dict[str, Any]:
    return {
        "sequence": receipt.sequence,
        "run_id": receipt.run_id,
        "event": receipt.event.value,
        "timestamp_ns": receipt.timestamp_ns,
        "action_id": receipt.action_id,
        "action_digest": receipt.action_digest,
        "detail": receipt.detail,
    }


def _message(result: ScenarioResult) -> str:
    if result.finding:
        return f"{result.finding.code}: {result.finding.title}"
    return result.detail or result.status.value


def _details(result: ScenarioResult) -> str:
    if result.finding is None:
        return result.detail or ""
    return (
        f"Expected: {result.finding.expected}\n"
        f"Observed: {result.finding.observed}\n"
        f"Remediation: {result.finding.remediation}"
    )


def _write(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
