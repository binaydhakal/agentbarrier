from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentbarrier.models import Decision
from agentbarrier.runtime import RuntimeStatus, SQLiteRuntimeStore
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
