"""Shared SQL runtime state and the durable SQLite implementation."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypeVar, cast

from agentbarrier.errors import (
    ActionBindingError,
    ActionInProgress,
    ActionLimitExceeded,
    ActionLimitValueError,
    ActionOutcomeUnknown,
    ApprovalAuthorizationError,
    ApprovalExpired,
    ApprovalRejected,
    ApprovalRequired,
    EmergencyPauseActive,
    InvalidActionState,
    PolicyDenied,
)
from agentbarrier.models import Decision, JsonValue
from agentbarrier.runtime.models import (
    ClaimOutcome,
    DecisionAuthorization,
    ExecutionClaim,
    PolicyDecision,
    PolicyEffect,
    RuntimeAction,
    RuntimeControlEvent,
    RuntimeControlReceipt,
    RuntimeEvent,
    RuntimeLimit,
    RuntimeLimitUsage,
    RuntimePause,
    RuntimeReceipt,
    RuntimeReconciliation,
    RuntimeRequest,
    RuntimeStatus,
    canonical_json,
    detached_json_object,
)

_SCHEMA_VERSION = "5"
_GLOBAL_SCOPE = ""
_MAX_SQL_INTEGER = 9_223_372_036_854_775_807


class _SQLRow(Protocol):
    def __getitem__(self, key: str) -> Any: ...


class _SQLCursor(Protocol):
    rowcount: int

    def fetchone(self) -> _SQLRow | None: ...

    def fetchall(self) -> list[_SQLRow]: ...


class _SQLConnection(Protocol):
    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> _SQLCursor: ...

    def close(self) -> None: ...


_StoreT = TypeVar("_StoreT", bound="_SQLRuntimeStore")


class _SQLRuntimeStore:
    """Shared transaction-safe runtime behavior for supported SQL databases."""

    _connection: _SQLConnection
    _sqlite_connection: sqlite3.Connection

    def __init__(
        self,
        path: str | Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        execution_lease_seconds: float = 300,
    ) -> None:
        self._configure_runtime_store(
            identifier=str(path),
            clock_ns=clock_ns,
            execution_lease_seconds=execution_lease_seconds,
        )
        sqlite_connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        sqlite_connection.row_factory = sqlite3.Row
        sqlite_connection.execute("PRAGMA journal_mode=WAL")
        sqlite_connection.execute("PRAGMA synchronous=FULL")
        sqlite_connection.execute("PRAGMA foreign_keys=ON")
        sqlite_connection.execute("PRAGMA busy_timeout=30000")
        self._sqlite_connection = sqlite_connection
        self._connection = cast(_SQLConnection, sqlite_connection)
        self._initialize_schema()

    def _configure_runtime_store(
        self,
        *,
        identifier: str,
        clock_ns: Callable[[], int],
        execution_lease_seconds: float,
    ) -> None:
        if not math.isfinite(execution_lease_seconds) or execution_lease_seconds <= 0:
            raise ValueError("execution_lease_seconds must be finite and greater than zero")
        self.path = identifier
        self._clock_ns = clock_ns
        self._execution_lease_ns = int(execution_lease_seconds * 1_000_000_000)
        if self._execution_lease_ns < 1:
            raise ValueError("execution_lease_seconds must be at least one nanosecond")
        self._lock = threading.RLock()

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
            if previous_version not in {"1", "2", "3", "4", _SCHEMA_VERSION}:
                raise RuntimeError(
                    f"unsupported runtime schema version {previous_version!r}; "
                    f"expected {_SCHEMA_VERSION!r}"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_actions (
                    action_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL DEFAULT 'default',
                    requested_by TEXT,
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
                    approval_ttl_ns INTEGER,
                    execution_lease_expires_at_ns INTEGER,
                    result_json TEXT,
                    error TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    UNIQUE (namespace, tool_name, idempotency_key)
                )
                """
            )
            if previous_version != _SCHEMA_VERSION:
                columns = {
                    str(item["name"])
                    for item in self._connection.execute(
                        "PRAGMA table_info(runtime_actions)"
                    ).fetchall()
                }
                if "organization_id" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runtime_actions "
                        "ADD COLUMN organization_id TEXT NOT NULL DEFAULT 'default'"
                    )
                if "requested_by" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runtime_actions ADD COLUMN requested_by TEXT"
                    )
                if previous_version == "1" and "execution_lease_expires_at_ns" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runtime_actions "
                        "ADD COLUMN execution_lease_expires_at_ns INTEGER"
                    )
                if previous_version == "1":
                    self._connection.execute(
                        """
                        UPDATE runtime_actions
                        SET execution_lease_expires_at_ns = updated_at_ns
                        WHERE status = ? AND execution_lease_expires_at_ns IS NULL
                        """,
                        (RuntimeStatus.EXECUTING.value,),
                    )
                if "approval_ttl_ns" not in columns:
                    self._connection.execute(
                        "ALTER TABLE runtime_actions ADD COLUMN approval_ttl_ns INTEGER"
                    )
                self._connection.execute(
                    """
                    UPDATE runtime_actions
                    SET approval_ttl_ns = expires_at_ns - created_at_ns
                    WHERE policy_effect = ? AND expires_at_ns IS NOT NULL
                        AND expires_at_ns > created_at_ns AND approval_ttl_ns IS NULL
                    """,
                    (PolicyEffect.REQUIRE_APPROVAL.value,),
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
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_pauses (
                    namespace TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    paused_at_ns INTEGER NOT NULL,
                    paused_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (namespace, tool_name)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_limits (
                    limit_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    window_ns INTEGER NOT NULL,
                    max_actions INTEGER,
                    value_argument TEXT,
                    max_value INTEGER,
                    enabled INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    CHECK (window_ns > 0),
                    CHECK (max_actions IS NULL OR max_actions > 0),
                    CHECK (max_value IS NULL OR max_value > 0),
                    CHECK ((value_argument IS NULL) = (max_value IS NULL)),
                    CHECK (max_actions IS NOT NULL OR max_value IS NOT NULL),
                    CHECK (enabled IN (0, 1))
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_limit_usage (
                    limit_id TEXT NOT NULL,
                    window_started_at_ns INTEGER NOT NULL,
                    actions_used INTEGER NOT NULL,
                    value_used INTEGER NOT NULL,
                    PRIMARY KEY (limit_id, window_started_at_ns),
                    FOREIGN KEY (limit_id) REFERENCES runtime_limits(limit_id),
                    CHECK (window_started_at_ns >= 0),
                    CHECK (actions_used >= 0),
                    CHECK (value_used >= 0)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_limit_reservations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL,
                    limit_id TEXT NOT NULL,
                    window_started_at_ns INTEGER NOT NULL,
                    actions_reserved INTEGER NOT NULL,
                    value_reserved INTEGER NOT NULL,
                    released_at_ns INTEGER,
                    FOREIGN KEY (action_id) REFERENCES runtime_actions(action_id),
                    FOREIGN KEY (limit_id) REFERENCES runtime_limits(limit_id),
                    CHECK (window_started_at_ns >= 0),
                    CHECK (actions_reserved > 0),
                    CHECK (value_reserved >= 0)
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_limits_match
                ON runtime_limits (enabled, namespace, tool_name)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_limit_reservations_active
                ON runtime_limit_reservations (action_id, released_at_ns)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_control_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    timestamp_ns INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT,
                    receipt_hash TEXT NOT NULL
                )
                """
            )
            if previous_version != _SCHEMA_VERSION:
                self._connection.execute(
                    "UPDATE runtime_metadata SET value = ? WHERE key = 'schema_version'",
                    (_SCHEMA_VERSION,),
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
                try:
                    self._connection.execute("COMMIT")
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise

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
            approval_ttl_ns = (
                int(decision.approval_ttl_seconds * 1_000_000_000)
                if decision.effect is PolicyEffect.REQUIRE_APPROVAL
                and decision.approval_ttl_seconds is not None
                else None
            )
            expires_at_ns = now + approval_ttl_ns if approval_ttl_ns is not None else None
            arguments_json = canonical_json(dict(request.arguments), path="arguments")
            decided_by = "policy" if decision.effect is PolicyEffect.ALLOW else None
            self._connection.execute(
                """
                INSERT INTO runtime_actions (
                    action_id, organization_id, requested_by, namespace, tool_name,
                    arguments_json, idempotency_key,
                    request_digest, policy_version, policy_rule, policy_effect, status,
                    created_at_ns, updated_at_ns, expires_at_ns, approval_ttl_ns, decided_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.action_id,
                    request.organization_id,
                    request.requested_by,
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
                    approval_ttl_ns,
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
        if not isinstance(decision, Decision):
            raise TypeError("decision must be an AgentBarrier Decision")
        now = self._clock_ns()
        with self._transaction():
            row = self._refresh_state(self._require_row(action_id), now=now)
            return self._decide_locked(
                self._row_to_action(row),
                decision,
                decided_by=decided_by,
                reason=reason,
                now=now,
            )

    def decide_authorized(
        self,
        action_id: str,
        decision: Decision,
        *,
        authorization: DecisionAuthorization,
        reason: str | None = None,
    ) -> RuntimeAction:
        """Decide only after enforcing tenant, namespace, role, and self-review constraints."""

        if not isinstance(decision, Decision):
            raise TypeError("decision must be an AgentBarrier Decision")
        now = self._clock_ns()
        with self._transaction():
            row = self._refresh_state(self._require_row(action_id), now=now)
            action = self._row_to_action(row)
            if action.organization_id != authorization.organization_id:
                raise ApprovalAuthorizationError(
                    "organization_mismatch",
                    "the reviewer cannot access this organization's action",
                )
            if action.namespace not in authorization.namespaces:
                raise ApprovalAuthorizationError(
                    "namespace_forbidden",
                    "the reviewer cannot access this action namespace",
                )
            if decision not in authorization.decisions:
                raise ApprovalAuthorizationError(
                    "decision_forbidden",
                    "the reviewer is not allowed to make this decision",
                )
            if (
                authorization.require_separate_approver
                and action.requested_by is not None
                and action.requested_by == (authorization.reviewer_subject or authorization.actor)
            ):
                raise ApprovalAuthorizationError(
                    "separation_of_duties",
                    "the requester cannot approve or reject their own action",
                )
            return self._decide_locked(
                action,
                decision,
                decided_by=authorization.actor,
                reason=reason,
                now=now,
            )

    def _decide_locked(
        self,
        action: RuntimeAction,
        decision: Decision,
        *,
        decided_by: str,
        reason: str | None,
        now: int,
    ) -> RuntimeAction:
        target = RuntimeStatus.APPROVED if decision is Decision.APPROVE else RuntimeStatus.REJECTED
        if action.status is target:
            return action
        if action.status is RuntimeStatus.EXPIRED:
            raise ApprovalExpired(action)
        if action.status is not RuntimeStatus.PENDING:
            raise InvalidActionState(
                f"action {action.action_id!r} cannot be {decision.value}d "
                f"from {action.status.value}"
            )
        self._connection.execute(
            """
            UPDATE runtime_actions
            SET status = ?, updated_at_ns = ?, decided_by = ?, decision_reason = ?
            WHERE action_id = ?
            """,
            (target.value, now, decided_by, reason, action.action_id),
        )
        self._append_receipt(
            action_id=action.action_id,
            event=RuntimeEvent.APPROVED if decision is Decision.APPROVE else RuntimeEvent.REJECTED,
            timestamp_ns=now,
            request_digest=action.request_digest,
            actor=decided_by,
            detail=reason,
        )
        return self._row_to_action(self._require_row(action.action_id))

    def set_pause(
        self,
        *,
        paused_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
    ) -> RuntimePause:
        """Set or replace an emergency pause for one exact control scope."""

        actor = self._require_text(paused_by, name="paused_by")
        detail = self._require_text(reason, name="reason")
        stored_namespace = self._normalize_scope_value(namespace, name="namespace")
        stored_tool = self._normalize_scope_value(tool_name, name="tool_name")
        scope = self._format_scope(stored_namespace, stored_tool)
        now = self._clock_ns()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO runtime_pauses (
                    namespace, tool_name, paused_at_ns, paused_by, reason
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (namespace, tool_name) DO UPDATE SET
                    paused_at_ns = excluded.paused_at_ns,
                    paused_by = excluded.paused_by,
                    reason = excluded.reason
                """,
                (stored_namespace, stored_tool, now, actor, detail),
            )
            self._append_control_receipt(
                event=RuntimeControlEvent.EMERGENCY_PAUSE_SET,
                timestamp_ns=now,
                actor=actor,
                scope=scope,
                detail=detail,
            )
            row = self._connection.execute(
                "SELECT * FROM runtime_pauses WHERE namespace = ? AND tool_name = ?",
                (stored_namespace, stored_tool),
            ).fetchone()
            if row is None:  # pragma: no cover - insert and read share one transaction
                raise RuntimeError("emergency pause was not persisted")
            return self._row_to_pause(row)

    def clear_pause(
        self,
        *,
        resumed_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
    ) -> bool:
        """Clear one exact emergency pause scope, returning whether it existed."""

        actor = self._require_text(resumed_by, name="resumed_by")
        detail = self._require_text(reason, name="reason")
        stored_namespace = self._normalize_scope_value(namespace, name="namespace")
        stored_tool = self._normalize_scope_value(tool_name, name="tool_name")
        scope = self._format_scope(stored_namespace, stored_tool)
        now = self._clock_ns()
        with self._transaction():
            deleted = self._connection.execute(
                "DELETE FROM runtime_pauses WHERE namespace = ? AND tool_name = ?",
                (stored_namespace, stored_tool),
            ).rowcount
            if not deleted:
                return False
            self._append_control_receipt(
                event=RuntimeControlEvent.EMERGENCY_PAUSE_CLEARED,
                timestamp_ns=now,
                actor=actor,
                scope=scope,
                detail=detail,
            )
            return True

    def list_pauses(self) -> tuple[RuntimePause, ...]:
        """Return active emergency pauses from broadest to narrowest scope."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM runtime_pauses
                ORDER BY (namespace != '') + (tool_name != ''), namespace, tool_name
                """
            ).fetchall()
        return tuple(self._row_to_pause(row) for row in rows)

    def configure_limit(
        self,
        limit_id: str,
        *,
        window_seconds: float,
        updated_by: str,
        reason: str,
        namespace: str | None = None,
        tool_name: str | None = None,
        max_actions: int | None = None,
        value_argument: str | None = None,
        max_value: int | None = None,
    ) -> RuntimeLimit:
        """Create or safely update an atomic fixed-window execution limit."""

        normalized_id = self._require_text(limit_id, name="limit_id")
        actor = self._require_text(updated_by, name="updated_by")
        update_reason = self._require_text(reason, name="reason")
        stored_namespace = self._normalize_scope_value(namespace, name="namespace")
        stored_tool = self._normalize_scope_value(tool_name, name="tool_name")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and greater than zero")
        window_ns = int(window_seconds * 1_000_000_000)
        if window_ns < 1:
            raise ValueError("window_seconds must be at least one nanosecond")
        if window_ns > _MAX_SQL_INTEGER:
            raise ValueError("window_seconds exceeds the supported 64-bit integer range")
        if max_actions is not None and (
            isinstance(max_actions, bool)
            or not isinstance(max_actions, int)
            or max_actions < 1
            or max_actions > _MAX_SQL_INTEGER
        ):
            raise ValueError("max_actions must be a positive integer within SQLite's range")
        if max_value is not None and (
            isinstance(max_value, bool)
            or not isinstance(max_value, int)
            or max_value < 1
            or max_value > _MAX_SQL_INTEGER
        ):
            raise ValueError("max_value must be a positive integer within SQLite's range")
        if (value_argument is None) != (max_value is None):
            raise ValueError("value_argument and max_value must be configured together")
        normalized_argument = (
            self._normalize_value_argument(value_argument) if value_argument is not None else None
        )
        if max_actions is None and max_value is None:
            raise ValueError("at least one of max_actions or max_value is required")

        now = self._clock_ns()
        scope = self._format_scope(stored_namespace, stored_tool)
        with self._transaction():
            existing = self._connection.execute(
                "SELECT * FROM runtime_limits WHERE limit_id = ?", (normalized_id,)
            ).fetchone()
            if existing is not None:
                immutable_existing = (
                    str(existing["namespace"]),
                    str(existing["tool_name"]),
                    int(existing["window_ns"]),
                    str(existing["value_argument"])
                    if existing["value_argument"] is not None
                    else None,
                )
                immutable_requested = (
                    stored_namespace,
                    stored_tool,
                    window_ns,
                    normalized_argument,
                )
                if immutable_existing != immutable_requested:
                    raise ValueError(
                        "an existing limit's scope, window, and value_argument are immutable; "
                        "disable it and use a new limit_id"
                    )
            self._connection.execute(
                """
                INSERT INTO runtime_limits (
                    limit_id, namespace, tool_name, window_ns, max_actions,
                    value_argument, max_value, enabled, updated_at_ns, updated_by, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (limit_id) DO UPDATE SET
                    max_actions = excluded.max_actions,
                    max_value = excluded.max_value,
                    enabled = 1,
                    updated_at_ns = excluded.updated_at_ns,
                    updated_by = excluded.updated_by,
                    reason = excluded.reason
                """,
                (
                    normalized_id,
                    stored_namespace,
                    stored_tool,
                    window_ns,
                    max_actions,
                    normalized_argument,
                    max_value,
                    now,
                    actor,
                    update_reason,
                ),
            )
            self._append_control_receipt(
                event=RuntimeControlEvent.LIMIT_CONFIGURED,
                timestamp_ns=now,
                actor=actor,
                scope=scope,
                detail=canonical_json(
                    {
                        "limit_id": normalized_id,
                        "max_actions": max_actions,
                        "max_value": max_value,
                        "reason": update_reason,
                        "value_argument": normalized_argument,
                        "window_ns": window_ns,
                    },
                    path="limit configuration",
                ),
            )
            return self._row_to_limit(self._require_limit_row(normalized_id))

    def disable_limit(self, limit_id: str, *, updated_by: str, reason: str) -> RuntimeLimit:
        """Disable a configured limit without erasing its usage or audit history."""

        normalized_id = self._require_text(limit_id, name="limit_id")
        actor = self._require_text(updated_by, name="updated_by")
        update_reason = self._require_text(reason, name="reason")
        now = self._clock_ns()
        with self._transaction():
            existing = self._require_limit_row(normalized_id)
            if not bool(existing["enabled"]):
                return self._row_to_limit(existing)
            self._connection.execute(
                """
                UPDATE runtime_limits
                SET enabled = 0, updated_at_ns = ?, updated_by = ?, reason = ?
                WHERE limit_id = ?
                """,
                (now, actor, update_reason, normalized_id),
            )
            scope = self._format_scope(str(existing["namespace"]), str(existing["tool_name"]))
            self._append_control_receipt(
                event=RuntimeControlEvent.LIMIT_DISABLED,
                timestamp_ns=now,
                actor=actor,
                scope=scope,
                detail=canonical_json(
                    {"limit_id": normalized_id, "reason": update_reason},
                    path="limit disable",
                ),
            )
            return self._row_to_limit(self._require_limit_row(normalized_id))

    def list_limits(self) -> tuple[RuntimeLimit, ...]:
        """Return all configured limits, including disabled definitions."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runtime_limits ORDER BY limit_id"
            ).fetchall()
        return tuple(self._row_to_limit(row) for row in rows)

    def limit_usage(self, limit_id: str | None = None) -> tuple[RuntimeLimitUsage, ...]:
        """Return current-window usage for one or all configured limits."""

        normalized_id = (
            self._require_text(limit_id, name="limit_id") if limit_id is not None else None
        )
        now = self._clock_ns()
        with self._lock:
            if normalized_id is None:
                limit_rows = self._connection.execute(
                    "SELECT * FROM runtime_limits ORDER BY limit_id"
                ).fetchall()
            else:
                limit_rows = [self._require_limit_row(normalized_id)]
            usage: list[RuntimeLimitUsage] = []
            for limit_row in limit_rows:
                window_ns = int(limit_row["window_ns"])
                window_started_at_ns = now - (now % window_ns)
                row = self._connection.execute(
                    """
                    SELECT * FROM runtime_limit_usage
                    WHERE limit_id = ? AND window_started_at_ns = ?
                    """,
                    (limit_row["limit_id"], window_started_at_ns),
                ).fetchone()
                usage.append(
                    RuntimeLimitUsage(
                        limit_id=str(limit_row["limit_id"]),
                        window_started_at_ns=window_started_at_ns,
                        actions_used=int(row["actions_used"]) if row is not None else 0,
                        value_used=int(row["value_used"]) if row is not None else 0,
                    )
                )
        return tuple(usage)

    def control_receipts(self) -> tuple[RuntimeControlReceipt, ...]:
        """Return ordered emergency-control and limit-configuration receipts."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runtime_control_receipts ORDER BY sequence"
            ).fetchall()
        return tuple(self._row_to_control_receipt(row) for row in rows)

    def verify_control_receipt_chain(self) -> bool:
        """Verify every operator-control receipt link and payload digest."""

        previous_hash: str | None = None
        for receipt in self.control_receipts():
            if receipt.previous_hash != previous_hash:
                return False
            expected = self._calculate_control_receipt_hash(
                event=receipt.event,
                timestamp_ns=receipt.timestamp_ns,
                actor=receipt.actor,
                scope=receipt.scope,
                detail=receipt.detail,
                previous_hash=receipt.previous_hash,
            )
            if receipt.receipt_hash != expected:
                return False
            previous_hash = receipt.receipt_hash
        return True

    def claim(self, action_id: str, *, request_digest: str) -> ExecutionClaim:
        """Atomically claim an approval or replay a completed result."""

        now = self._clock_ns()
        blocked: EmergencyPauseActive | ActionLimitExceeded | ActionLimitValueError | None = None
        claim: ExecutionClaim | None = None
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
                pause = self._matching_pause(action)
                if pause is not None:
                    scope = self._format_scope(str(pause["namespace"]), str(pause["tool_name"]))
                    reason = str(pause["reason"])
                    blocked = EmergencyPauseActive(action, scope=scope, reason=reason)
                    self._append_receipt(
                        action_id=action_id,
                        event=RuntimeEvent.EMERGENCY_PAUSE_BLOCKED,
                        timestamp_ns=now,
                        request_digest=request_digest,
                        actor="runtime",
                        detail=canonical_json(
                            {"reason": reason, "scope": scope}, path="pause block"
                        ),
                    )
                else:
                    blocked = self._reserve_limit_capacity(action, now=now)
                    if blocked is not None:
                        self._append_receipt(
                            action_id=action_id,
                            event=RuntimeEvent.LIMIT_BLOCKED,
                            timestamp_ns=now,
                            request_digest=request_digest,
                            actor="runtime",
                            detail=self._limit_block_detail(blocked),
                        )
                if blocked is None:
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
                    claim = ExecutionClaim(ClaimOutcome.EXECUTE, claimed)
            elif action.status is RuntimeStatus.PENDING:
                raise ApprovalRequired(action)
            elif action.status is RuntimeStatus.DENIED:
                raise PolicyDenied(action)
            elif action.status is RuntimeStatus.REJECTED:
                raise ApprovalRejected(action)
            elif action.status is RuntimeStatus.EXPIRED:
                raise ApprovalExpired(action)
            elif action.status is RuntimeStatus.EXECUTING:
                raise ActionInProgress(action)
            elif action.status is RuntimeStatus.UNKNOWN:
                raise ActionOutcomeUnknown(action)
            elif action.status is not RuntimeStatus.APPROVED:
                raise InvalidActionState(
                    f"action {action_id!r} cannot execute from {action.status.value}"
                )
        if blocked is not None:
            raise blocked
        if claim is None:  # pragma: no cover - all lifecycle branches assign or raise
            raise RuntimeError("approved action was not claimed")
        return claim

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
                decided_by = action.decided_by
                decision_reason = action.decision_reason
                event = RuntimeEvent.RECONCILIATION_COMMITTED
            elif action.policy_effect is PolicyEffect.REQUIRE_APPROVAL:
                status = RuntimeStatus.PENDING
                expires_at_ns = (
                    now + action.approval_ttl_ns if action.approval_ttl_ns is not None else None
                )
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

            if outcome is RuntimeReconciliation.NOT_COMMITTED:
                self._release_limit_capacity(action.action_id, released_at_ns=now)

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

    @property
    def schema_version(self) -> str:
        """Return the migrated runtime database schema version."""

        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM runtime_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:  # pragma: no cover - initialization always creates the row
            raise RuntimeError("runtime database has no schema version")
        return str(row["value"])

    def backup(self, destination: str | Path) -> Path:
        """Write a consistent, integrity-checked backup without replacing an existing file."""

        destination_path = Path(destination).expanduser()
        if destination_path.parent == Path(""):
            destination_path = Path.cwd() / destination_path
        if not destination_path.parent.is_dir():
            raise FileNotFoundError(f"backup directory does not exist: {destination_path.parent}")
        source_path = Path(self.path).expanduser()
        if self.path != ":memory:" and source_path.resolve() == destination_path.resolve():
            raise ValueError("backup destination must be different from the runtime database")
        if destination_path.exists():
            raise FileExistsError(f"backup destination already exists: {destination_path}")

        created = False
        try:
            destination_path.touch(mode=0o600, exist_ok=False)
            created = True
            destination_path.chmod(0o600)
            with self._lock, closing(sqlite3.connect(destination_path)) as backup_connection:
                self._sqlite_connection.backup(backup_connection)
                integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]) != "ok":
                    raise RuntimeError("runtime database backup failed its integrity check")
        except BaseException:
            if created:
                destination_path.unlink(missing_ok=True)
            raise
        return destination_path

    def _matching_pause(self, action: RuntimeAction) -> _SQLRow | None:
        row = self._connection.execute(
            """
            SELECT * FROM runtime_pauses
            WHERE namespace IN (?, ?) AND tool_name IN (?, ?)
            ORDER BY (namespace != '') + (tool_name != '') DESC, paused_at_ns DESC
            LIMIT 1
            """,
            (_GLOBAL_SCOPE, action.namespace, _GLOBAL_SCOPE, action.tool_name),
        ).fetchone()
        return row

    def _reserve_limit_capacity(
        self,
        action: RuntimeAction,
        *,
        now: int,
    ) -> ActionLimitExceeded | ActionLimitValueError | None:
        rows = self._connection.execute(
            """
            SELECT * FROM runtime_limits
            WHERE enabled = 1
                AND namespace IN (?, ?)
                AND tool_name IN (?, ?)
            ORDER BY limit_id
            """,
            (_GLOBAL_SCOPE, action.namespace, _GLOBAL_SCOPE, action.tool_name),
        ).fetchall()
        reservations: list[tuple[str, int, int, int]] = []
        for row in rows:
            limit_id = str(row["limit_id"])
            window_ns = int(row["window_ns"])
            window_started_at_ns = now - (now % window_ns)
            value_argument = (
                str(row["value_argument"]) if row["value_argument"] is not None else None
            )
            value_requested = 0
            if value_argument is not None:
                extracted = self._extract_integer_argument(action.arguments, value_argument)
                if extracted is None:
                    return ActionLimitValueError(
                        action,
                        limit_id=limit_id,
                        value_argument=value_argument,
                    )
                value_requested = extracted
            usage = self._connection.execute(
                """
                SELECT actions_used, value_used FROM runtime_limit_usage
                WHERE limit_id = ? AND window_started_at_ns = ?
                """,
                (limit_id, window_started_at_ns),
            ).fetchone()
            actions_used = int(usage["actions_used"]) if usage is not None else 0
            value_used = int(usage["value_used"]) if usage is not None else 0
            max_actions = int(row["max_actions"]) if row["max_actions"] is not None else None
            max_value = int(row["max_value"]) if row["max_value"] is not None else None
            if max_actions is not None and actions_used + 1 > max_actions:
                return ActionLimitExceeded(
                    action,
                    limit_id=limit_id,
                    resource="actions",
                    used=actions_used,
                    requested=1,
                    maximum=max_actions,
                )
            if max_value is not None and value_used + value_requested > max_value:
                return ActionLimitExceeded(
                    action,
                    limit_id=limit_id,
                    resource=value_argument or "value",
                    used=value_used,
                    requested=value_requested,
                    maximum=max_value,
                )
            reservations.append((limit_id, window_started_at_ns, 1, value_requested))

        for limit_id, window_started_at_ns, actions_reserved, value_reserved in reservations:
            self._connection.execute(
                """
                INSERT INTO runtime_limit_usage (
                    limit_id, window_started_at_ns, actions_used, value_used
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (limit_id, window_started_at_ns) DO UPDATE SET
                    actions_used = runtime_limit_usage.actions_used + excluded.actions_used,
                    value_used = runtime_limit_usage.value_used + excluded.value_used
                """,
                (limit_id, window_started_at_ns, actions_reserved, value_reserved),
            )
            self._connection.execute(
                """
                INSERT INTO runtime_limit_reservations (
                    action_id, limit_id, window_started_at_ns,
                    actions_reserved, value_reserved, released_at_ns
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    action.action_id,
                    limit_id,
                    window_started_at_ns,
                    actions_reserved,
                    value_reserved,
                ),
            )
        return None

    def _release_limit_capacity(self, action_id: str, *, released_at_ns: int) -> None:
        reservations = self._connection.execute(
            """
            SELECT * FROM runtime_limit_reservations
            WHERE action_id = ? AND released_at_ns IS NULL
            ORDER BY sequence
            """,
            (action_id,),
        ).fetchall()
        for reservation in reservations:
            self._connection.execute(
                """
                UPDATE runtime_limit_usage
                SET actions_used = actions_used - ?, value_used = value_used - ?
                WHERE limit_id = ? AND window_started_at_ns = ?
                """,
                (
                    reservation["actions_reserved"],
                    reservation["value_reserved"],
                    reservation["limit_id"],
                    reservation["window_started_at_ns"],
                ),
            )
            self._connection.execute(
                """
                UPDATE runtime_limit_reservations
                SET released_at_ns = ?
                WHERE sequence = ?
                """,
                (released_at_ns, reservation["sequence"]),
            )

    @staticmethod
    def _extract_integer_argument(
        arguments: Mapping[str, JsonValue],
        dotted_path: str,
    ) -> int | None:
        current: object = arguments
        for segment in dotted_path.split("."):
            if not isinstance(current, Mapping) or segment not in current:
                return None
            current = current[segment]
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            return None
        return current

    @staticmethod
    def _limit_block_detail(error: ActionLimitExceeded | ActionLimitValueError) -> str:
        if isinstance(error, ActionLimitExceeded):
            payload: dict[str, JsonValue] = {
                "limit_id": error.limit_id,
                "maximum": error.maximum,
                "requested": error.requested,
                "resource": error.resource,
                "used": error.used,
            }
        else:
            payload = {
                "limit_id": error.limit_id,
                "resource": error.value_argument,
                "error": "invalid_or_missing_non_negative_integer",
            }
        return canonical_json(payload, path="limit block")

    def _append_control_receipt(
        self,
        *,
        event: RuntimeControlEvent,
        timestamp_ns: int,
        actor: str,
        scope: str,
        detail: str,
    ) -> None:
        previous = self._connection.execute(
            "SELECT receipt_hash FROM runtime_control_receipts ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous["receipt_hash"]) if previous is not None else None
        receipt_hash = self._calculate_control_receipt_hash(
            event=event,
            timestamp_ns=timestamp_ns,
            actor=actor,
            scope=scope,
            detail=detail,
            previous_hash=previous_hash,
        )
        self._connection.execute(
            """
            INSERT INTO runtime_control_receipts (
                event, timestamp_ns, actor, scope, detail, previous_hash, receipt_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event.value, timestamp_ns, actor, scope, detail, previous_hash, receipt_hash),
        )

    @staticmethod
    def _calculate_control_receipt_hash(
        *,
        event: RuntimeControlEvent,
        timestamp_ns: int,
        actor: str,
        scope: str,
        detail: str,
        previous_hash: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "event": event.value,
                "timestamp_ns": timestamp_ns,
                "actor": actor,
                "scope": scope,
                "detail": detail,
                "previous_hash": previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_text(value: str, *, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"{name} must not contain control characters")
        return value

    @classmethod
    def _normalize_scope_value(cls, value: str | None, *, name: str) -> str:
        return _GLOBAL_SCOPE if value is None else cls._require_text(value, name=name)

    @classmethod
    def _normalize_value_argument(cls, value: str) -> str:
        normalized = cls._require_text(value, name="value_argument")
        if any(not segment or segment.strip() != segment for segment in normalized.split(".")):
            raise ValueError("value_argument must be a dot-separated object path")
        return normalized

    @staticmethod
    def _format_scope(namespace: str, tool_name: str) -> str:
        return canonical_json(
            {
                "namespace": namespace or None,
                "tool_name": tool_name or None,
            },
            path="control scope",
        )

    def _refresh_expiry(self, row: _SQLRow, *, now: int) -> _SQLRow:
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

    def _refresh_state(self, row: _SQLRow, *, now: int) -> _SQLRow:
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

    def _require_row(self, action_id: str) -> _SQLRow:
        row = self._connection.execute(
            "SELECT * FROM runtime_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime action {action_id!r}")
        return row

    def _require_limit_row(self, limit_id: str) -> _SQLRow:
        row = self._connection.execute(
            "SELECT * FROM runtime_limits WHERE limit_id = ?", (limit_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime limit {limit_id!r}")
        return row

    @staticmethod
    def _row_to_action(row: _SQLRow) -> RuntimeAction:
        result_json = row["result_json"]
        return RuntimeAction(
            action_id=str(row["action_id"]),
            organization_id=str(row["organization_id"]),
            requested_by=(str(row["requested_by"]) if row["requested_by"] is not None else None),
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
            approval_ttl_ns=(
                int(row["approval_ttl_ns"]) if row["approval_ttl_ns"] is not None else None
            ),
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
    def _row_to_receipt(row: _SQLRow) -> RuntimeReceipt:
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

    @staticmethod
    def _row_to_pause(row: _SQLRow) -> RuntimePause:
        namespace = str(row["namespace"])
        tool_name = str(row["tool_name"])
        return RuntimePause(
            namespace=namespace or None,
            tool_name=tool_name or None,
            paused_at_ns=int(row["paused_at_ns"]),
            paused_by=str(row["paused_by"]),
            reason=str(row["reason"]),
        )

    @staticmethod
    def _row_to_limit(row: _SQLRow) -> RuntimeLimit:
        namespace = str(row["namespace"])
        tool_name = str(row["tool_name"])
        return RuntimeLimit(
            limit_id=str(row["limit_id"]),
            namespace=namespace or None,
            tool_name=tool_name or None,
            window_ns=int(row["window_ns"]),
            max_actions=(int(row["max_actions"]) if row["max_actions"] is not None else None),
            value_argument=(
                str(row["value_argument"]) if row["value_argument"] is not None else None
            ),
            max_value=(int(row["max_value"]) if row["max_value"] is not None else None),
            enabled=bool(row["enabled"]),
            updated_at_ns=int(row["updated_at_ns"]),
            updated_by=str(row["updated_by"]),
            reason=str(row["reason"]),
        )

    @staticmethod
    def _row_to_control_receipt(row: _SQLRow) -> RuntimeControlReceipt:
        return RuntimeControlReceipt(
            sequence=int(row["sequence"]),
            event=RuntimeControlEvent(str(row["event"])),
            timestamp_ns=int(row["timestamp_ns"]),
            actor=str(row["actor"]),
            scope=str(row["scope"]),
            detail=str(row["detail"]),
            previous_hash=(str(row["previous_hash"]) if row["previous_hash"] is not None else None),
            receipt_hash=str(row["receipt_hash"]),
        )

    def close(self) -> None:
        """Close the database connection."""

        with self._lock:
            self._connection.close()

    def __enter__(self: _StoreT) -> _StoreT:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class SQLiteRuntimeStore(_SQLRuntimeStore):
    """Concurrency-safe runtime state stored in one SQLite database."""
