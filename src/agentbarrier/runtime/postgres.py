"""PostgreSQL runtime store with the same invariants as the SQLite backend."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from agentbarrier.errors import RuntimeStoreError
from agentbarrier.runtime.models import PolicyEffect, RuntimeStatus
from agentbarrier.runtime.store import (
    _SCHEMA_VERSION,
    _SQLConnection,
    _SQLCursor,
    _SQLRuntimeStore,
)

_SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class _RawPostgresConnection(Protocol):
    def execute(
        self,
        statement: object,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def close(self) -> None: ...


class _PostgresConnection:
    """Translate the shared SQL subset and serialize invariant-changing transactions."""

    def __init__(
        self,
        connection: _RawPostgresConnection,
        *,
        advisory_lock_key: int,
        lock_timeout_ms: int,
        database_error: type[BaseException],
    ) -> None:
        self._connection = connection
        self._advisory_lock_key = advisory_lock_key
        self._lock_timeout = f"{lock_timeout_ms}ms"
        self._database_error = database_error

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> _SQLCursor:
        normalized = statement.strip().upper()
        if normalized == "BEGIN IMMEDIATE":
            try:
                self._connection.execute("BEGIN")
                self._connection.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (self._lock_timeout,),
                )
                cursor = self._connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (self._advisory_lock_key,),
                )
            except self._database_error as error:
                self._connection.execute("ROLLBACK")
                raise RuntimeStoreError("PostgreSQL runtime transaction lock failed") from error
            return cast(_SQLCursor, cursor)
        translated = statement.replace("?", "%s").replace(
            "(namespace != '') + (tool_name != '')",
            "(CASE WHEN namespace <> '' THEN 1 ELSE 0 END) + "
            "(CASE WHEN tool_name <> '' THEN 1 ELSE 0 END)",
        )
        try:
            cursor = self._connection.execute(translated, parameters or None)
        except self._database_error as error:
            raise RuntimeStoreError("PostgreSQL runtime operation failed") from error
        return cast(_SQLCursor, cursor)

    def close(self) -> None:
        self._connection.close()


class PostgresRuntimeStore(_SQLRuntimeStore):
    """Concurrency-safe runtime state in one dedicated PostgreSQL schema.

    All invariant-changing transactions take one schema-specific transaction-level advisory lock.
    This deliberately matches SQLite's serialized write behavior while preserving cross-process
    correctness for action binding, execution claims, limits, and global receipt chains.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "agentbarrier",
        create_schema: bool = False,
        migrate: bool = False,
        clock_ns: Callable[[], int] = time.time_ns,
        execution_lease_seconds: float = 300,
        lock_timeout_seconds: float = 30,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in dsn):
            raise ValueError("PostgreSQL DSN must not contain control characters")
        if _SCHEMA_PATTERN.fullmatch(schema) is None:
            raise ValueError(
                "PostgreSQL schema must be a lowercase unquoted identifier of 1 to 63 characters"
            )
        if create_schema and not migrate:
            raise ValueError("create_schema requires migrate=True")
        if not math.isfinite(lock_timeout_seconds) or lock_timeout_seconds <= 0:
            raise ValueError("PostgreSQL lock timeout must be finite and greater than zero")
        lock_timeout_ms = max(1, math.ceil(lock_timeout_seconds * 1_000))
        if lock_timeout_ms > 10 * 60 * 1_000:
            raise ValueError("PostgreSQL lock timeout must not exceed ten minutes")

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:  # pragma: no cover - exercised in a clean core install
            raise ImportError(
                "PostgreSQL dependencies are unavailable; install 'agentbarrier[postgres]'"
            ) from error

        self._configure_runtime_store(
            identifier="<postgresql>",
            clock_ns=clock_ns,
            execution_lease_seconds=execution_lease_seconds,
        )
        self.schema = schema
        try:
            raw_connection = cast(
                _RawPostgresConnection,
                psycopg.connect(dsn, autocommit=True, row_factory=dict_row),
            )
        except psycopg.Error as error:
            raise RuntimeStoreError("PostgreSQL runtime store connection failed") from error
        advisory_lock_key = int.from_bytes(
            sha256(f"agentbarrier-runtime:{schema}".encode("ascii")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        try:
            if create_schema:
                raw_connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            raw_connection.execute(f'SET search_path TO "{schema}", pg_catalog')
            self._connection = cast(
                _SQLConnection,
                _PostgresConnection(
                    raw_connection,
                    advisory_lock_key=advisory_lock_key,
                    lock_timeout_ms=lock_timeout_ms,
                    database_error=psycopg.Error,
                ),
            )
            if migrate:
                self._initialize_schema()
            else:
                self._validate_schema()
        except psycopg.Error as error:
            raw_connection.close()
            raise RuntimeStoreError(
                f"PostgreSQL runtime schema {schema!r} could not be accessed or migrated"
            ) from error
        except BaseException:
            raw_connection.close()
            raise

    def _validate_schema(self) -> None:
        relation = self._connection.execute(
            "SELECT to_regclass(?) AS relation",
            (f"{self.schema}.runtime_metadata",),
        ).fetchone()
        if relation is None or relation["relation"] is None:
            raise RuntimeStoreError(
                f"PostgreSQL runtime schema {self.schema!r} is missing; "
                "run 'agentbarrier database migrate' first"
            )
        row = self._connection.execute(
            "SELECT value FROM runtime_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or str(row["value"]) != _SCHEMA_VERSION:
            observed = None if row is None else str(row["value"])
            raise RuntimeStoreError(
                f"PostgreSQL runtime schema requires migration: observed {observed!r}, "
                f"expected {_SCHEMA_VERSION!r}"
            )
        backend = self._connection.execute(
            "SELECT value FROM runtime_metadata WHERE key = 'backend'"
        ).fetchone()
        if backend is None or str(backend["value"]) != "postgresql":
            raise RuntimeStoreError("runtime schema is not owned by the PostgreSQL backend")

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
            if previous_version not in {"1", "2", "3", _SCHEMA_VERSION}:
                raise RuntimeStoreError(
                    f"unsupported PostgreSQL runtime schema version {previous_version!r}; "
                    f"expected {_SCHEMA_VERSION!r}"
                )
            self._connection.execute(
                """
                INSERT INTO runtime_metadata (key, value)
                VALUES ('backend', 'postgresql')
                ON CONFLICT (key) DO NOTHING
                """
            )
            backend = self._connection.execute(
                "SELECT value FROM runtime_metadata WHERE key = 'backend'"
            ).fetchone()
            if backend is None or str(backend["value"]) != "postgresql":
                raise RuntimeStoreError("runtime schema is not owned by the PostgreSQL backend")

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
                    created_at_ns BIGINT NOT NULL,
                    updated_at_ns BIGINT NOT NULL,
                    expires_at_ns BIGINT,
                    approval_ttl_ns BIGINT,
                    execution_lease_expires_at_ns BIGINT,
                    result_json TEXT,
                    error TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    UNIQUE (namespace, tool_name, idempotency_key)
                )
                """
            )
            if previous_version in {"1", "2"}:
                self._connection.execute(
                    "ALTER TABLE runtime_actions "
                    "ADD COLUMN IF NOT EXISTS execution_lease_expires_at_ns BIGINT"
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
                self._connection.execute(
                    "ALTER TABLE runtime_actions ADD COLUMN IF NOT EXISTS approval_ttl_ns BIGINT"
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

            self._create_current_tables()
            if previous_version != _SCHEMA_VERSION:
                self._connection.execute(
                    "UPDATE runtime_metadata SET value = ? WHERE key = 'schema_version'",
                    (_SCHEMA_VERSION,),
                )

    def _create_current_tables(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS runtime_receipts (
                sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                action_id TEXT NOT NULL REFERENCES runtime_actions(action_id),
                event TEXT NOT NULL,
                timestamp_ns BIGINT NOT NULL,
                request_digest TEXT NOT NULL,
                actor TEXT,
                detail TEXT,
                previous_hash TEXT,
                receipt_hash TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_pauses (
                namespace TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                paused_at_ns BIGINT NOT NULL,
                paused_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (namespace, tool_name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_limits (
                limit_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                window_ns BIGINT NOT NULL CHECK (window_ns > 0),
                max_actions BIGINT CHECK (max_actions IS NULL OR max_actions > 0),
                value_argument TEXT,
                max_value BIGINT CHECK (max_value IS NULL OR max_value > 0),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                updated_at_ns BIGINT NOT NULL,
                updated_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                CHECK ((value_argument IS NULL) = (max_value IS NULL)),
                CHECK (max_actions IS NOT NULL OR max_value IS NOT NULL)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_limit_usage (
                limit_id TEXT NOT NULL REFERENCES runtime_limits(limit_id),
                window_started_at_ns BIGINT NOT NULL CHECK (window_started_at_ns >= 0),
                actions_used BIGINT NOT NULL CHECK (actions_used >= 0),
                value_used BIGINT NOT NULL CHECK (value_used >= 0),
                PRIMARY KEY (limit_id, window_started_at_ns)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_limit_reservations (
                sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                action_id TEXT NOT NULL REFERENCES runtime_actions(action_id),
                limit_id TEXT NOT NULL REFERENCES runtime_limits(limit_id),
                window_started_at_ns BIGINT NOT NULL CHECK (window_started_at_ns >= 0),
                actions_reserved BIGINT NOT NULL CHECK (actions_reserved > 0),
                value_reserved BIGINT NOT NULL CHECK (value_reserved >= 0),
                released_at_ns BIGINT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_limits_match
            ON runtime_limits (enabled, namespace, tool_name)
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_limit_reservations_active
            ON runtime_limit_reservations (action_id, released_at_ns)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_control_receipts (
                sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                event TEXT NOT NULL,
                timestamp_ns BIGINT NOT NULL,
                actor TEXT NOT NULL,
                scope TEXT NOT NULL,
                detail TEXT NOT NULL,
                previous_hash TEXT,
                receipt_hash TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            self._connection.execute(statement)

    def backup(self, destination: str | Path) -> Path:
        """Reject SQLite-style file backups; PostgreSQL deployments must use pg_dump."""

        del destination
        raise NotImplementedError("PostgreSQL backups must use pg_dump or managed snapshots")
