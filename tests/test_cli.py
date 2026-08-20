from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from agentbarrier import __version__
from agentbarrier.cli import main
from agentbarrier.runtime import PolicyDecision, PolicyEffect, RuntimeRequest, RuntimeStatus
from agentbarrier.runtime.store import SQLiteRuntimeStore


def test_cli_version_matches_distribution_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert __version__ == version("agentbarrier")
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentbarrier {version('agentbarrier')}"


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
    assert "11 passed" in capsys.readouterr().out
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


def test_cli_selects_and_reports_approval_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    json_path = tmp_path / "profile.json"
    assert (
        main(
            [
                "self-test",
                "--scenario",
                "parallel_barrier",
                "--approval-profile",
                "per-action",
                "--json",
                str(json_path),
            ]
        )
        == 0
    )

    assert "approval-profile=per-action" in capsys.readouterr().out
    assert json.loads(json_path.read_text())["approval_profile"] == "per-action"


def test_cli_generates_and_checks_compatibility_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_path = tmp_path / "compatibility.json"
    markdown_path = tmp_path / "compatibility.md"
    markdown_path.write_text(
        "# Matrix\n\n<!-- agentbarrier:compatibility:start -->\n"
        "stale\n<!-- agentbarrier:compatibility:end -->\n"
    )
    arguments = [
        "compatibility",
        "--adapter",
        "reference",
        "--json",
        str(json_path),
        "--markdown",
        str(markdown_path),
        "--strict-missing",
    ]

    assert main(arguments) == 0
    assert main([*arguments, "--check"]) == 0
    evidence = json.loads(json_path.read_text())
    assert evidence["adapters"][0]["key"] == "reference"
    assert evidence["profiles"] == ["run-wide", "per-action"]

    json_path.write_text("{}\n")
    assert main([*arguments, "--check"]) == 1
    assert "compatibility outputs are out of date" in capsys.readouterr().err
    assert json_path.read_text() == "{}\n"


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


def _pending_runtime_action(path: Path, *, action_id: str = "runtime-action") -> str:
    request = RuntimeRequest(
        action_id=action_id,
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": "refund-1", "amount": 100},
        idempotency_key="refund-1",
        policy_version="1",
        created_at_ns=1,
    )
    with SQLiteRuntimeStore(path) as store:
        store.submit(
            request,
            PolicyDecision(PolicyEffect.REQUIRE_APPROVAL, "review refunds", "1"),
        )
    return request.action_id


def _unknown_runtime_action(path: Path) -> str:
    request = RuntimeRequest(
        action_id="unknown-action",
        namespace="billing",
        tool_name="payments.refund",
        arguments={"request_id": "unknown-refund", "amount": 100},
        idempotency_key="unknown-refund",
        policy_version="1",
        created_at_ns=1,
    )
    with SQLiteRuntimeStore(path) as store:
        action = store.submit(
            request,
            PolicyDecision(PolicyEffect.ALLOW, "allow", "1"),
        )
        store.claim(action.action_id, request_digest=request.request_digest)
        store.mark_unknown(
            action.action_id,
            request_digest=request.request_digest,
            error="ConnectionError",
        )
    return request.action_id


def test_runtime_approval_cli_lists_shows_approves_and_audits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    action_id = _pending_runtime_action(path)

    assert main(["approvals", "list", "--db", str(path), "--status", "pending", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["action_id"] == action_id
    assert listed[0]["status"] == "pending"

    assert main(["approvals", "show", action_id, "--db", str(path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["arguments"] == {"amount": 100, "request_id": "refund-1"}

    assert (
        main(
            [
                "approvals",
                "approve",
                action_id,
                "--db",
                str(path),
                "--decided-by",
                "alice",
                "--reason",
                "ticket-123",
            ]
        )
        == 0
    )
    assert f"approved {action_id}" in capsys.readouterr().out
    with SQLiteRuntimeStore(path) as store:
        action = store.get_action(action_id)
        assert action.status is RuntimeStatus.APPROVED
        assert action.decided_by == "alice"

    assert main(["audit", "--db", str(path), "--action-id", action_id, "--json"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["chain_valid"] is True
    assert [item["event"] for item in audit["receipts"]] == [
        "approval_requested",
        "approved",
    ]


def test_runtime_approval_cli_rejects_and_handles_empty_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    assert main(["approvals", "list", "--db", str(path)]) == 0
    assert "No runtime actions" in capsys.readouterr().out

    action_id = _pending_runtime_action(path, action_id="reject-me")
    assert (
        main(
            [
                "approvals",
                "reject",
                action_id,
                "--db",
                str(path),
                "--decided-by",
                "bob",
            ]
        )
        == 0
    )
    assert "rejected reject-me" in capsys.readouterr().out


def test_runtime_cli_normalizes_unknown_action_to_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["approvals", "show", "missing", "--db", str(tmp_path / "runtime.db")])
    assert exc.value.code == 2


def test_runtime_cli_reconciles_committed_unknown_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "runtime.db"
    action_id = _unknown_runtime_action(path)
    assert (
        main(
            [
                "approvals",
                "reconcile",
                action_id,
                "--db",
                str(path),
                "--outcome",
                "committed",
                "--resolved-by",
                "payment-ledger",
                "--reason",
                "transaction exists",
                "--result-json",
                '{"status":"refunded"}',
            ]
        )
        == 0
    )
    assert "reconciled unknown-action as committed" in capsys.readouterr().out
    with SQLiteRuntimeStore(path) as store:
        assert store.get_action(action_id).status is RuntimeStatus.SUCCEEDED


def test_runtime_cli_requires_reconciliation_result(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    action_id = _unknown_runtime_action(path)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "approvals",
                "reconcile",
                action_id,
                "--db",
                str(path),
                "--outcome",
                "committed",
                "--resolved-by",
                "ledger",
                "--reason",
                "present",
            ]
        )
    assert exc.value.code == 2
