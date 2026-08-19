"""Durable, external observation of sentinel side effects."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from agentbarrier.models import (
    AuditEvent,
    AuditReceipt,
    EffectEvent,
    EffectPhase,
    JsonValue,
)


class EffectJournal:
    """Thread-safe SQLite journal that remains outside adapter/framework state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS effect_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                phase TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                detail TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_receipts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                action_id TEXT,
                action_digest TEXT,
                detail TEXT
            )
            """
        )

    def record(
        self,
        *,
        run_id: str,
        action_id: str,
        tool_name: str,
        phase: EffectPhase,
        arguments: dict[str, JsonValue],
        detail: str | None = None,
    ) -> EffectEvent:
        """Append and durably flush one observation."""

        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        timestamp_ns = time.time_ns()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO effect_events (
                    run_id, action_id, tool_name, phase, arguments_json, timestamp_ns, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, action_id, tool_name, phase.value, encoded, timestamp_ns, detail),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite guarantees it for INSERT
                raise RuntimeError("SQLite did not return an event sequence")
            sequence = int(cursor.lastrowid)
        return EffectEvent(
            sequence=sequence,
            run_id=run_id,
            action_id=action_id,
            tool_name=tool_name,
            phase=phase,
            arguments=json.loads(encoded),
            timestamp_ns=timestamp_ns,
            detail=detail,
        )

    def events(
        self,
        *,
        run_id: str | None = None,
        phase: EffectPhase | None = None,
    ) -> tuple[EffectEvent, ...]:
        """Return an ordered, immutable snapshot of matching events."""

        with self._lock:
            if run_id is not None and phase is not None:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, action_id, tool_name, phase, arguments_json,
                           timestamp_ns, detail
                    FROM effect_events
                    WHERE run_id = ? AND phase = ?
                    ORDER BY sequence
                    """,
                    (run_id, phase.value),
                ).fetchall()
            elif run_id is not None:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, action_id, tool_name, phase, arguments_json,
                           timestamp_ns, detail
                    FROM effect_events
                    WHERE run_id = ?
                    ORDER BY sequence
                    """,
                    (run_id,),
                ).fetchall()
            elif phase is not None:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, action_id, tool_name, phase, arguments_json,
                           timestamp_ns, detail
                    FROM effect_events
                    WHERE phase = ?
                    ORDER BY sequence
                    """,
                    (phase.value,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, action_id, tool_name, phase, arguments_json,
                           timestamp_ns, detail
                    FROM effect_events
                    ORDER BY sequence
                    """
                ).fetchall()
        return tuple(
            EffectEvent(
                sequence=int(row[0]),
                run_id=str(row[1]),
                action_id=str(row[2]),
                tool_name=str(row[3]),
                phase=EffectPhase(row[4]),
                arguments=json.loads(row[5]),
                timestamp_ns=int(row[6]),
                detail=row[7],
            )
            for row in rows
        )

    def committed(self, *, run_id: str | None = None) -> tuple[EffectEvent, ...]:
        """Return committed effects only."""

        return self.events(run_id=run_id, phase=EffectPhase.COMMITTED)

    def record_receipt(
        self,
        *,
        run_id: str,
        event: AuditEvent,
        action_id: str | None = None,
        action_digest: str | None = None,
        detail: str | None = None,
    ) -> AuditReceipt:
        """Append one durable control-plane transition."""

        timestamp_ns = time.time_ns()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO audit_receipts (
                    run_id, event, timestamp_ns, action_id, action_digest, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, event.value, timestamp_ns, action_id, action_digest, detail),
            )
            if cursor.lastrowid is None:  # pragma: no cover - SQLite guarantees it for INSERT
                raise RuntimeError("SQLite did not return a receipt sequence")
            sequence = int(cursor.lastrowid)
        return AuditReceipt(
            sequence=sequence,
            run_id=run_id,
            event=event,
            timestamp_ns=timestamp_ns,
            action_id=action_id,
            action_digest=action_digest,
            detail=detail,
        )

    def receipts(self, *, run_id: str | None = None) -> tuple[AuditReceipt, ...]:
        """Return an ordered snapshot of adapter control receipts."""

        with self._lock:
            if run_id is not None:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, event, timestamp_ns, action_id, action_digest, detail
                    FROM audit_receipts
                    WHERE run_id = ?
                    ORDER BY sequence
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT sequence, run_id, event, timestamp_ns, action_id, action_digest, detail
                    FROM audit_receipts
                    ORDER BY sequence
                    """
                ).fetchall()
        return tuple(
            AuditReceipt(
                sequence=int(row[0]),
                run_id=str(row[1]),
                event=AuditEvent(row[2]),
                timestamp_ns=int(row[3]),
                action_id=row[4],
                action_digest=row[5],
                detail=row[6],
            )
            for row in rows
        )

    def close(self) -> None:
        """Flush and close the journal connection."""

        with self._lock:
            self._connection.close()

    def __enter__(self) -> EffectJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
