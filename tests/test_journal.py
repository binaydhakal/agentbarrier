from __future__ import annotations

from pathlib import Path

import pytest

from agentbarrier.journal import EffectJournal
from agentbarrier.models import AuditEvent, EffectPhase


def test_journal_records_and_filters_durable_events(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    with EffectJournal(path) as journal:
        started = journal.record(
            run_id="run-1",
            action_id="action-1",
            tool_name="write",
            phase=EffectPhase.STARTED,
            arguments={"value": 1},
        )
        committed = journal.record(
            run_id="run-1",
            action_id="action-1",
            tool_name="write",
            phase=EffectPhase.COMMITTED,
            arguments={"value": 1},
        )
        journal.record(
            run_id="run-2",
            action_id="action-2",
            tool_name="write",
            phase=EffectPhase.COMMITTED,
            arguments={"value": 2},
            detail="other",
        )
        receipt = journal.record_receipt(
            run_id="run-1",
            event=AuditEvent.APPROVED,
            action_id="action-1",
            action_digest="abc123",
        )

        assert started.sequence < committed.sequence
        assert [event.phase for event in journal.events(run_id="run-1")] == [
            EffectPhase.STARTED,
            EffectPhase.COMMITTED,
        ]
        assert [event.action_id for event in journal.committed(run_id="run-1")] == ["action-1"]
        assert len(journal.events(phase=EffectPhase.COMMITTED)) == 2
        assert receipt.sequence == 1
        assert journal.receipts(run_id="run-1") == (receipt,)
        assert len(journal.receipts()) == 1

    assert path.exists()


def test_journal_rejects_non_json_numbers(tmp_path: Path) -> None:
    with EffectJournal(tmp_path / "events.sqlite3") as journal, pytest.raises(ValueError):
        journal.record(
            run_id="run",
            action_id="action",
            tool_name="write",
            phase=EffectPhase.STARTED,
            arguments={"bad": float("nan")},
        )
