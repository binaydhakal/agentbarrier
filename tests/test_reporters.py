from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.models import Capability
from agentbarrier.reporters import (
    render_console,
    suite_to_dict,
    write_json,
    write_junit,
    write_sarif,
)
from agentbarrier.runner import RunnerOptions, SuiteRunner
from tests.helpers import UnsafeAdapter


def test_all_report_formats_are_well_formed(tmp_path: Path) -> None:
    suite = SuiteRunner().verify_sync(ReferenceAdapter())
    json_path = tmp_path / "nested" / "report.json"
    junit_path = tmp_path / "report.xml"
    sarif_path = tmp_path / "report.sarif"

    write_json(suite, json_path)
    write_junit(suite, junit_path)
    write_sarif(suite, sarif_path)

    json_report = json.loads(json_path.read_text())
    junit = ET.parse(junit_path).getroot()
    sarif = json.loads(sarif_path.read_text())
    assert json_report["schema_version"] == "1.0"
    assert json_report["summary"]["passed"] == 10
    assert junit.tag == "testsuites"
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["results"] == []
    assert "10 passed" in render_console(suite)
    audit_result = next(item for item in json_report["results"] if item["id"] == "audit_receipts")
    assert audit_result["receipts"]


def test_failure_reports_include_structured_finding(tmp_path: Path) -> None:
    suite = SuiteRunner(
        RunnerOptions(settle_seconds=0.01, scenarios=("approval_hold",))
    ).verify_sync(UnsafeAdapter("early", Capability.APPROVAL))
    junit_path = tmp_path / "failure.xml"
    sarif_path = tmp_path / "failure.sarif"
    write_junit(suite, junit_path)
    write_sarif(suite, sarif_path)

    report = suite_to_dict(suite)
    sarif = json.loads(sarif_path.read_text())
    junit_text = junit_path.read_text()
    plain_console = render_console(suite)
    color_console = render_console(suite, color=True)
    assert report["results"][0]["finding"]["code"] == "AB002"
    assert sarif["runs"][0]["results"][0]["ruleId"] == "AB002"
    assert "AB002" in junit_text
    assert "Effect committed before approval" in plain_console
    assert "Expected: zero committed effects" in plain_console
    assert "Fix: Move the approval barrier" in plain_console
    assert "\x1b[31;1mFAIL" in color_console
    assert "\x1b[31;1mAB002" in color_console
