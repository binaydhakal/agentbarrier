from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbarrier.cli import main


def test_self_test_cli_writes_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    json_path = tmp_path / "report.json"
    junit_path = tmp_path / "report.xml"
    sarif_path = tmp_path / "report.sarif"
    exit_code = main(
        [
            "self-test",
            "--json",
            str(json_path),
            "--junit",
            str(junit_path),
            "--sarif",
            str(sarif_path),
        ]
    )

    assert exit_code == 0
    assert "10 passed" in capsys.readouterr().out
    assert json.loads(json_path.read_text())["passed"] is True
    assert junit_path.exists()
    assert sarif_path.exists()


def test_verify_cli_loads_class_and_scenario_selection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "verify",
            "agentbarrier.adapters.reference:ReferenceAdapter",
            "--scenario",
            "approval_hold",
        ]
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "approval_hold" in output
    assert "1 passed" in output


def test_scenarios_cli_lists_guarantees(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scenarios"]) == 0
    output = capsys.readouterr().out
    assert "approval_hold" in output
    assert "parallel_barrier" in output
    assert "outcome_ambiguity" in output
    assert "audit_receipts" in output


def test_cli_can_force_color(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["self-test", "--scenario", "approval_hold", "--color", "always"]) == 0
    assert "\x1b[32;1mPASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "target",
    [
        "missing-syntax",
        "agentbarrier.adapters.reference:missing",
        "agentbarrier.models:Capability",
    ],
)
def test_verify_cli_rejects_invalid_targets(target: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["verify", target])
    assert exc.value.code == 2
