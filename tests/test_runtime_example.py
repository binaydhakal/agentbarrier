from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentbarrier.models import Decision
from agentbarrier.runtime import RuntimeStatus, SQLiteRuntimeStore
from examples import runtime_refund
from examples.runtime_refund import main


def test_runtime_refund_example_pauses_executes_and_replays_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"
    arguments = [
        "--db",
        str(runtime_path),
        "--ledger",
        str(ledger_path),
        "--request-id",
        "refund-1001",
        "--account-id",
        "account-7",
        "--amount",
        "100",
    ]

    assert main(arguments) == 3
    with SQLiteRuntimeStore(runtime_path) as store:
        pending = store.list_actions(status=RuntimeStatus.PENDING)
        assert len(pending) == 1
        store.decide(pending[0].action_id, Decision.APPROVE, decided_by="reviewer")

    assert main(arguments) == 0
    assert main(arguments) == 0
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refunds").fetchone() == (1,)
        assert connection.execute(
            "SELECT request_id, account_id, amount FROM refunds"
        ).fetchone() == ("refund-1001", "account-7", 100)


def test_runtime_refund_example_allows_small_refund(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"
    assert (
        main(
            [
                "--db",
                str(runtime_path),
                "--ledger",
                str(ledger_path),
                "--request-id",
                "refund-small",
                "--account-id",
                "account-1",
                "--amount",
                "5",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "refunded"


def test_runtime_refund_example_interactively_approves_and_executes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"
    arguments = [
        "--db",
        str(runtime_path),
        "--ledger",
        str(ledger_path),
        "--request-id",
        "refund-interactive",
        "--account-id",
        "account-8",
        "--amount",
        "100",
    ]
    responses = iter(["invalid", "a", "alice", "ticket-789"])
    monkeypatch.setattr(runtime_refund, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert "This exact refund requires your approval:" in output
    assert "Choose a, r, or l." in output
    assert "executing the refund once" in output
    with SQLiteRuntimeStore(runtime_path) as store:
        action = store.list_actions()[0]
        assert action.status is RuntimeStatus.SUCCEEDED
        assert action.decided_by == "alice"
        assert action.decision_reason == "ticket-789"
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refunds").fetchone() == (1,)

    assert main(arguments) == 0
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refunds").fetchone() == (1,)


def test_runtime_refund_example_interactively_rejects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"
    responses = iter(["r", "bob", "amount not authorized"])
    monkeypatch.setattr(runtime_refund, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))

    assert (
        main(
            [
                "--db",
                str(runtime_path),
                "--ledger",
                str(ledger_path),
                "--request-id",
                "refund-rejected",
                "--account-id",
                "account-9",
                "--amount",
                "100",
            ]
        )
        == 4
    )
    assert "the refund was not executed" in capsys.readouterr().out
    assert not ledger_path.exists()
    with SQLiteRuntimeStore(runtime_path) as store:
        action = store.list_actions()[0]
        assert action.status is RuntimeStatus.REJECTED
        assert action.decided_by == "bob"


def test_runtime_refund_example_can_leave_action_pending(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.db"
    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setattr(runtime_refund, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "l")

    assert (
        main(
            [
                "--db",
                str(runtime_path),
                "--ledger",
                str(ledger_path),
                "--request-id",
                "refund-later",
                "--account-id",
                "account-10",
                "--amount",
                "100",
            ]
        )
        == 3
    )
    assert "Review later with:" in capsys.readouterr().out
    assert not ledger_path.exists()
    with SQLiteRuntimeStore(runtime_path) as store:
        assert store.list_actions()[0].status is RuntimeStatus.PENDING
