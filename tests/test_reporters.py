from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.models import ApprovalBarrierProfile, Capability
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
    assert json_report["schema_version"] == "1.1"
    assert json_report["approval_profile"] == "run-wide"
    assert json_report["summary"]["passed"] == 11
    assert junit.tag == "testsuites"
    junit_property = junit.find("./testsuite/properties/property")
    assert junit_property is not None
    assert junit_property.attrib == {
        "name": "agentbarrier.approval_profile",
        "value": "run-wide",
    }
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["properties"]["approvalProfile"] == "run-wide"
    assert sarif["runs"][0]["results"] == []
    assert "11 passed" in render_console(suite)
    assert "approval-profile=run-wide" in render_console(suite)
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


def test_reports_preserve_the_selected_per_action_profile(tmp_path: Path) -> None:
    suite = SuiteRunner(
        RunnerOptions(
            scenarios=("parallel_barrier",),
            approval_profile=ApprovalBarrierProfile.PER_ACTION,
        )
    ).verify_sync(ReferenceAdapter())
    junit_path = tmp_path / "profile.xml"
    sarif_path = tmp_path / "profile.sarif"

    write_junit(suite, junit_path)
    write_sarif(suite, sarif_path)

    junit = ET.parse(junit_path).getroot()
    junit_property = junit.find("./testsuite/properties/property")
    sarif = json.loads(sarif_path.read_text())
    assert suite_to_dict(suite)["approval_profile"] == "per-action"
    assert junit_property is not None
    assert junit_property.attrib["value"] == "per-action"
    assert sarif["runs"][0]["properties"]["approvalProfile"] == "per-action"
    assert "approval-profile=per-action" in render_console(suite)
