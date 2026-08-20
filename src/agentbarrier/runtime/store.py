"""Durable SQLite state and integrity-linked receipts for runtime actions."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import cast

from agentbarrier.errors import (
    ActionBindingError,
    ActionInProgress,
    ActionOutcomeUnknown,
    ApprovalExpired,
    ApprovalRejected,
    ApprovalRequired,
    InvalidActionState,
    PolicyDenied,
)
from agentbarrier.models import Decision, JsonValue
from agentbarrier.runtime.models import (
    ClaimOutcome,
    ExecutionClaim,
    PolicyDecision,
    PolicyEffect,
    RuntimeAction,
    RuntimeEvent,
    RuntimeReceipt,
    RuntimeReconciliation,
    RuntimeRequest,
    RuntimeStatus,
    canonical_json,
    detached_json_object,
)

_SCHEMA_VERSION = "2"


class SQLiteRuntimeStore:
    """Concurrency-safe runtime state stored in one SQLite database."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        execution_lease_seconds: float = 300,
    ) -> None:
        if not math.isfinite(execution_lease_seconds) or execution_lease_seconds <= 0:
            raise ValueError("execution_lease_seconds must be finite and greater than zero")
        self.path = str(path)
        self._clock_ns = clock_ns
        self._execution_lease_ns = int(execution_lease_seconds * 1_000_000_000)
        if self._execution_lease_ns < 1:
            raise ValueError("execution_lease_seconds must be at least one nanosecond")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM runtime_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO runtime_metadata (key, value) VALUES ('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
                previous_version = _SCHEMA_VERSION
            else:
                previous_version = str(row["value"])
            if previous_version not in {"1", _SCHEMA_VERSION}:
                raise RuntimeError(
                    f"unsupported runtime schema version {previous_version!r}; "
                    f"expected {_SCHEMA_VERSION!r}"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_actions (
                    action_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    policy_rule TEXT NOT NULL,
                    policy_effect TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    expires_at_ns INTEGER,
                    execution_lease_expires_at_ns INTEGER,
                    result_json TEXT,
                    error TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    UNIQUE (namespace, tool_name, idempotency_key)
                )
                """
            )
            if previous_version == "1":
                columns = {
                    str(item["name"])
                    for item in self._connection.execute(
                        "PRAGMA table_info(runtime_actions)"
                    ).fetchall()
                }
                if "execution_lease_expires_at_ns" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runtime_actions "
                        "ADD COLUMN execution_lease_expires_at_ns INTEGER"
                    )
                self._connection.execute(
                    """
                    UPDATE runtime_actions
                    SET execution_lease_expires_at_ns = updated_at_ns
                    WHERE status = ? AND execution_lease_expires_at_ns IS NULL
                    """,
                    (RuntimeStatus.EXECUTING.value,),
                )
                self._connection.execute(
                    "UPDATE runtime_metadata SET value = ? WHERE key = 'schema_version'",
                    (_SCHEMA_VERSION,),
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    timestamp_ns INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    actor TEXT,
                    detail TEXT,
                    previous_hash TEXT,
                    receipt_hash TEXT NOT NULL,
                    FOREIGN KEY (action_id) REFERENCES runtime_actions(action_id)
                )
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def submit(self, request: RuntimeRequest, decision: PolicyDecision) -> RuntimeAction:
        """Create an action once, or return the exact existing idempotent action."""

        if decision.policy_version != request.policy_version:
            raise ValueError("policy decision version does not match the runtime request")
        now = self._clock_ns()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT * FROM runtime_actions
                WHERE namespace = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (request.namespace, request.tool_name, request.idempotency_key),
            ).fetchone()
            if row is not None:
                row = self._refresh_state(row, now=now)
                if str(row["request_digest"]) != request.request_digest:
                    raise ActionBindingError(
                        "idempotency key was already bound to a different tool request or "
                        "policy version"
                    )
                return self._row_to_action(row)

            status = {
                PolicyEffect.ALLOW: RuntimeStatus.APPROVED,
                PolicyEffect.DENY: RuntimeStatus.DENIED,
                PolicyEffect.REQUIRE_APPROVAL: RuntimeStatus.PENDING,
            }[decision.effect]
            expires_at_ns = (
                now + int(decision.approval_ttl_seconds * 1_000_000_000)
                if decision.effect is PolicyEffect.REQUIRE_APPROVAL
                and decision.approval_ttl_seconds is not None
                else None
            )
            arguments_json = canonical_json(dict(request.arguments), path="arguments")
            decided_by = "policy" if decision.effect is PolicyEffect.ALLOW else None
            self._connection.execute(
                """
                INSERT INTO runtime_actions (
                    action_id, namespace, tool_name, arguments_json, idempotency_key,
                    request_digest, policy_version, policy_rule, policy_effect, status,
                    created_at_ns, updated_at_ns, expires_at_ns, decided_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.action_id,
                    request.namespace,
                    request.tool_name,
                    arguments_json,
                    request.idempotency_key,
                    request.request_digest,
                    request.policy_version,
                    decision.rule_name,
                    decision.effect.value,
                    status.value,
                    request.created_at_ns,
                    now,
                    expires_at_ns,
                    decided_by,
                ),
            )
            event = {
                PolicyEffect.ALLOW: RuntimeEvent.POLICY_ALLOWED,
                PolicyEffect.DENY: RuntimeEvent.POLICY_DENIED,
                PolicyEffect.REQUIRE_APPROVAL: RuntimeEvent.APPROVAL_REQUESTED,
            }[decision.effect]
            self._append_receipt(
                action_id=request.action_id,
                event=event,
                timestamp_ns=now,
                request_digest=request.request_digest,
                actor="policy",
                detail=f"rule={decision.rule_name};policy={decision.policy_version}",
            )
            return self._row_to_action(self._require_row(request.action_id))

    def decide(
        self,
        action_id: str,
        decision: Decision,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> RuntimeAction:
        """Approve or reject one pending exact request."""

        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        now = self._clock_ns()
        with self._transaction():
            row = self._refresh_state(self._require_row(action_id), now=now)
            action = self._row_to_action(row)
            target = (
                RuntimeStatus.APPROVED if decision is Decision.APPROVE else RuntimeStatus.REJECTED
            )
            if action.status is target:
                return action
            if action.status is RuntimeStatus.EXPIRED:
                raise ApprovalExpired(action)
            if action.status is not RuntimeStatus.PENDING:
                raise InvalidActionState(
                    f"action {action_id!r} cannot be {decision.value}d from {action.status.value}"
                )
            self._connection.execute(
                """
                UPDATE runtime_actions
                SET status = ?, updated_at_ns = ?, decided_by = ?, decision_reason = ?
                WHERE action_id = ?
                """,
                (target.value, now, decided_by, reason, action_id),
            )
            self._append_receipt(
                action_id=action_id,
                event=(
                    RuntimeEvent.APPROVED if decision is Decision.APPROVE else RuntimeEvent.REJECTED
                ),
                timestamp_ns=now,
                request_digest=action.request_digest,
                actor=decided_by,
                detail=reason,
            )
            return self._row_to_action(self._require_row(action_id))

    def claim(self, action_id: str, *, request_digest: str) -> ExecutionClaim:
        """Atomically claim an approval or replay a completed result."""

        now = self._clock_ns()
        with self._transaction():
            row = self._refresh_state(self._require_row(action_id), now=now)
            action = self._row_to_action(row)
            if action.request_digest != request_digest:
                raise ActionBindingError(
                    "execution request does not match the stored action digest"
                )
            if action.status is RuntimeStatus.SUCCEEDED:
                self._append_receipt(
                    action_id=action_id,
                    event=RuntimeEvent.RESULT_REPLAYED,
                    timestamp_ns=now,
                    request_digest=request_digest,
                    actor="runtime",
                    detail=None,
                )
                return ExecutionClaim(ClaimOutcome.REPLAY, action, action.result)
            if action.status is RuntimeStatus.APPROVED:
                self._connection.execute(
                    """
                    UPDATE runtime_actions
                    SET status = ?, updated_at_ns = ?, execution_lease_expires_at_ns = ?
                    WHERE action_id = ?
                    """,
                    (
                        RuntimeStatus.EXECUTING.value,
                        now,
                        now + self._execution_lease_ns,
                        action_id,
                    ),
                )
                self._append_receipt(
                    action_id=action_id,
                    event=RuntimeEvent.EXECUTION_STARTED,
                    timestamp_ns=now,
                    request_digest=request_digest,
                    actor="runtime",
                    detail=None,
                )
                claimed = self._row_to_action(self._require_row(action_id))
                return ExecutionClaim(ClaimOutcome.EXECUTE, claimed)
            if action.status is RuntimeStatus.PENDING:
                raise ApprovalRequired(action)
            if action.status is RuntimeStatus.DENIED:
                raise PolicyDenied(action)
            if action.status is RuntimeStatus.REJECTED:
                raise ApprovalRejected(action)
            if action.status is RuntimeStatus.EXPIRED:
                raise ApprovalExpired(action)
            if action.status is RuntimeStatus.EXECUTING:
                raise ActionInProgress(action)
            if action.status is RuntimeStatus.UNKNOWN:
                raise ActionOutcomeUnknown(action)
            raise InvalidActionState(
                f"action {action_id!r} cannot execute from {action.status.value}"
            )

    def complete(
        self,
        action_id: str,
        *,
        request_digest: str,
        result: JsonValue,
    ) -> RuntimeAction:
        """Persist a JSON-compatible result after the protected effect returns."""

        result_json = canonical_json(result, path="result")
        now = self._clock_ns()
        with self._transaction():
            action = self._row_to_action(self._require_row(action_id))
            self._require_executing(action, request_digest=request_digest)
            self._connection.execute(
                """
                UPDATE runtime_actions
                SET status = ?, updated_at_ns = ?, result_json = ?, error = NULL,
                    execution_lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (RuntimeStatus.SUCCEEDED.value, now, result_json, action_id),
            )
            self._append_receipt(
                action_id=action_id,
                event=RuntimeEvent.EXECUTION_SUCCEEDED,
                timestamp_ns=now,
                request_digest=request_digest,
                actor="runtime",
                detail=None,
            )
            return self._row_to_action(self._require_row(action_id))

    def mark_unknown(
        self,
        action_id: str,
        *,
        request_digest: str,
        error: str,
    ) -> RuntimeAction:
        """Fail closed after execution starts without a provable durable result."""

        if not error.strip():
            raise ValueError("error must not be empty")
        now = self._clock_ns()
        with self._transaction():
            action = self._row_to_action(self._require_row(action_id))
            self._require_executing(action, request_digest=request_digest)
            self._connection.execute(
                """
                UPDATE runtime_actions
                SET status = ?, updated_at_ns = ?, error = ?,
                    execution_lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (RuntimeStatus.UNKNOWN.value, now, error, action_id),
            )
            self._append_receipt(
                action_id=action_id,
                event=RuntimeEvent.EXECUTION_UNKNOWN,
                timestamp_ns=now,
                request_digest=request_digest,
                actor="runtime",
                detail=error,
            )
            return self._row_to_action(self._require_row(action_id))

    def reconcile(
        self,
        action_id: str,
        outcome: RuntimeReconciliation,
        *,
        resolved_by: str,
        reason: str,
        result: JsonValue = None,
    ) -> RuntimeAction:
        """Resolve an unknown outcome using external, identity-bound evidence."""

        if not resolved_by.strip():
            raise ValueError("resolved_by must not be empty")
        if not reason.strip():
            raise ValueError("reason must not be empty")
        if outcome is RuntimeReconciliation.NOT_COMMITTED and result is not None:
            raise ValueError("result is valid only for a committed reconciliation")
        result_json = (
            canonical_json(result, path="reconciliation result")
            if outcome is RuntimeReconciliation.COMMITTED
            else None
        )
        now = self._clock_ns()
        with self._transaction():
            action = self._row_to_action(self._refresh_state(self._require_row(action_id), now=now))
            if action.status is not RuntimeStatus.UNKNOWN:
                raise InvalidActionState(
                    f"action {action_id!r} cannot be reconciled from {action.status.value}"
                )

            if outcome is RuntimeReconciliation.COMMITTED:
                status = RuntimeStatus.SUCCEEDED
                expires_at_ns = None
                decided_by = resolved_by
                decision_reason = reason
                event = RuntimeEvent.RECONCILIATION_COMMITTED
            elif action.policy_effect is PolicyEffect.REQUIRE_APPROVAL:
                status = RuntimeStatus.PENDING
                original_ttl_ns = (
                    action.expires_at_ns - action.created_at_ns
                    if action.expires_at_ns is not None
                    else None
                )
                expires_at_ns = now + original_ttl_ns if original_ttl_ns is not None else None
                decided_by = None
                decision_reason = None
                event = RuntimeEvent.RECONCILIATION_NOT_COMMITTED
            elif action.policy_effect is PolicyEffect.ALLOW:
                status = RuntimeStatus.APPROVED
                expires_at_ns = None
                decided_by = "policy"
                decision_reason = None
                event = RuntimeEvent.RECONCILIATION_NOT_COMMITTED
            else:  # pragma: no cover - denied actions can never start execution
                raise InvalidActionState("a policy-denied action cannot have an unknown outcome")

            self._connection.execute(
                """
                UPDATE runtime_actions
                SET status = ?, updated_at_ns = ?, expires_at_ns = ?,
                    execution_lease_expires_at_ns = NULL, result_json = ?, error = NULL,
                    decided_by = ?, decision_reason = ?
                WHERE action_id = ?
                """,
                (
                    status.value,
                    now,
                    expires_at_ns,
                    result_json,
                    decided_by,
                    decision_reason,
                    action_id,
                ),
            )
            self._append_receipt(
                action_id=action_id,
                event=event,
                timestamp_ns=now,
                request_digest=action.request_digest,
                actor=resolved_by,
                detail=reason,
            )
            return self._row_to_action(self._require_row(action_id))

    def get_action(self, action_id: str) -> RuntimeAction:
        """Return one action, applying expiry before the snapshot is read."""

        with self._transaction():
            row = self._refresh_state(self._require_row(action_id), now=self._clock_ns())
            return self._row_to_action(row)

    def list_actions(self, *, status: RuntimeStatus | None = None) -> tuple[RuntimeAction, ...]:
        """Return actions ordered by creation, optionally filtered after expiry."""

        with self._transaction():
            rows = self._connection.execute(
                "SELECT * FROM runtime_actions ORDER BY created_at_ns, action_id"
            ).fetchall()
            now = self._clock_ns()
            actions = tuple(self._row_to_action(self._refresh_state(row, now=now)) for row in rows)
            if status is None:
                return actions
            return tuple(action for action in actions if action.status is status)

    def receipts(self, *, action_id: str | None = None) -> tuple[RuntimeReceipt, ...]:
        """Return ordered runtime audit receipts."""

        with self._lock:
            if action_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM runtime_receipts ORDER BY sequence"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM runtime_receipts WHERE action_id = ? ORDER BY sequence",
                    (action_id,),
                ).fetchall()
        return tuple(self._row_to_receipt(row) for row in rows)

    def verify_receipt_chain(self) -> bool:
        """Verify every global receipt link and payload digest."""

        previous_hash: str | None = None
        for receipt in self.receipts():
            if receipt.previous_hash != previous_hash:
                return False
            expected = self._calculate_receipt_hash(
                action_id=receipt.action_id,
                event=receipt.event,
                timestamp_ns=receipt.timestamp_ns,
                request_digest=receipt.request_digest,
                actor=receipt.actor,
                detail=receipt.detail,
                previous_hash=receipt.previous_hash,
            )
            if receipt.receipt_hash != expected:
                return False
            previous_hash = receipt.receipt_hash
        return True

    def _refresh_expiry(self, row: sqlite3.Row, *, now: int) -> sqlite3.Row:
        status = RuntimeStatus(str(row["status"]))
        expires_at = row["expires_at_ns"]
        if (
            status in {RuntimeStatus.PENDING, RuntimeStatus.APPROVED}
            and expires_at is not None
            and now >= int(expires_at)
        ):
            self._connection.execute(
                "UPDATE runtime_actions SET status = ?, updated_at_ns = ? WHERE action_id = ?",
                (RuntimeStatus.EXPIRED.value, now, row["action_id"]),
            )
            self._append_receipt(
                action_id=str(row["action_id"]),
                event=RuntimeEvent.EXPIRED,
                timestamp_ns=now,
                request_digest=str(row["request_digest"]),
                actor="runtime",
                detail=None,
            )
            return self._require_row(str(row["action_id"]))
        return row

    def _refresh_state(self, row: sqlite3.Row, *, now: int) -> sqlite3.Row:
        row = self._refresh_expiry(row, now=now)
        status = RuntimeStatus(str(row["status"]))
        lease_expires = row["execution_lease_expires_at_ns"]
        if (
            status is RuntimeStatus.EXECUTING
            and lease_expires is not None
            and now >= int(lease_expires)
        ):
            self._connection.execute(
                """
                UPDATE runtime_actions
                SET status = ?, updated_at_ns = ?, error = ?,
                    execution_lease_expires_at_ns = NULL
                WHERE action_id = ?
                """,
                (
                    RuntimeStatus.UNKNOWN.value,
                    now,
                    "ExecutionLeaseExpired",
                    row["action_id"],
                ),
            )
            self._append_receipt(
                action_id=str(row["action_id"]),
                event=RuntimeEvent.EXECUTION_ABANDONED,
                timestamp_ns=now,
                request_digest=str(row["request_digest"]),
                actor="runtime",
                detail="ExecutionLeaseExpired",
            )
            return self._require_row(str(row["action_id"]))
        return row

    def _require_executing(self, action: RuntimeAction, *, request_digest: str) -> None:
        if action.request_digest != request_digest:
            raise ActionBindingError("execution request does not match the stored action digest")
        if action.status is not RuntimeStatus.EXECUTING:
            raise InvalidActionState(
                f"action {action.action_id!r} is not executing; status is {action.status.value}"
            )

    def _append_receipt(
        self,
        *,
        action_id: str,
        event: RuntimeEvent,
        timestamp_ns: int,
        request_digest: str,
        actor: str | None,
        detail: str | None,
    ) -> None:
        previous = self._connection.execute(
            "SELECT receipt_hash FROM runtime_receipts ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous["receipt_hash"]) if previous is not None else None
        receipt_hash = self._calculate_receipt_hash(
            action_id=action_id,
            event=event,
            timestamp_ns=timestamp_ns,
            request_digest=request_digest,
            actor=actor,
            detail=detail,
            previous_hash=previous_hash,
        )
        self._connection.execute(
            """
            INSERT INTO runtime_receipts (
                action_id, event, timestamp_ns, request_digest, actor, detail,
                previous_hash, receipt_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                event.value,
                timestamp_ns,
                request_digest,
                actor,
                detail,
                previous_hash,
                receipt_hash,
            ),
        )

    @staticmethod
    def _calculate_receipt_hash(
        *,
        action_id: str,
        event: RuntimeEvent,
        timestamp_ns: int,
        request_digest: str,
        actor: str | None,
        detail: str | None,
        previous_hash: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "action_id": action_id,
                "event": event.value,
                "timestamp_ns": timestamp_ns,
                "request_digest": request_digest,
                "actor": actor,
                "detail": detail,
                "previous_hash": previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def _require_row(self, action_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM runtime_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime action {action_id!r}")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> RuntimeAction:
        result_json = row["result_json"]
        return RuntimeAction(
            action_id=str(row["action_id"]),
            namespace=str(row["namespace"]),
            tool_name=str(row["tool_name"]),
            arguments=detached_json_object(str(row["arguments_json"])),
            idempotency_key=str(row["idempotency_key"]),
            request_digest=str(row["request_digest"]),
            policy_version=str(row["policy_version"]),
            policy_rule=str(row["policy_rule"]),
            policy_effect=PolicyEffect(str(row["policy_effect"])),
            status=RuntimeStatus(str(row["status"])),
            created_at_ns=int(row["created_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            expires_at_ns=(int(row["expires_at_ns"]) if row["expires_at_ns"] is not None else None),
            execution_lease_expires_at_ns=(
                int(row["execution_lease_expires_at_ns"])
                if row["execution_lease_expires_at_ns"] is not None
                else None
            ),
            result=json.loads(str(result_json)) if result_json is not None else None,
            result_available=result_json is not None,
            error=str(row["error"]) if row["error"] is not None else None,
            decided_by=str(row["decided_by"]) if row["decided_by"] is not None else None,
            decision_reason=(
                str(row["decision_reason"]) if row["decision_reason"] is not None else None
            ),
        )

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> RuntimeReceipt:
        return RuntimeReceipt(
            sequence=int(row["sequence"]),
            action_id=str(row["action_id"]),
            event=RuntimeEvent(str(row["event"])),
            timestamp_ns=int(row["timestamp_ns"]),
            request_digest=str(row["request_digest"]),
            actor=str(row["actor"]) if row["actor"] is not None else None,
            detail=str(row["detail"]) if row["detail"] is not None else None,
            previous_hash=(str(row["previous_hash"]) if row["previous_hash"] is not None else None),
            receipt_hash=str(row["receipt_hash"]),
        )

    def close(self) -> None:
        """Close the SQLite connection."""

        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteRuntimeStore:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
