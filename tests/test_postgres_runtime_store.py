from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from hashlib import sha256

import pytest

from agentbarrier.cli import main
from agentbarrier.errors import RuntimeStoreError
from agentbarrier.runtime import (
    PolicyDecision,
    PolicyEffect,
    PostgresRuntimeStore,
    RuntimeRequest,
    RuntimeStatus,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTBARRIER_TEST_POSTGRES_DSN") is None,
    reason="AGENTBARRIER_TEST_POSTGRES_DSN is not configured",
)


class Clock:
    def __init__(self, value: int = 1_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


@pytest.fixture
def postgres_schema() -> Iterator[tuple[str, str]]:
    dsn = os.environ["AGENTBARRIER_TEST_POSTGRES_DSN"]
    schema = f"agentbarrier_test_{uuid.uuid4().hex}"
    try:
        yield dsn, schema
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def test_postgres_store_uses_dedicated_schema_and_rejects_file_backup(
    postgres_schema: tuple[str, str],
) -> None:
    import psycopg
    from psycopg import sql

    dsn, schema = postgres_schema
    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        create_schema=True,
        migrate=True,
    ) as store:
        assert store.path == "<postgresql>"
        assert store.schema == schema
        assert store.schema_version == "5"
        with pytest.raises(NotImplementedError, match="pg_dump"):
            store.backup("unused.db")

    with psycopg.connect(dsn, autocommit=True) as connection:
        backend = connection.execute(
            sql.SQL("SELECT value FROM {}.runtime_metadata WHERE key = 'backend'").format(
                sql.Identifier(schema)
            )
        ).fetchone()
    assert backend == ("postgresql",)


@pytest.mark.parametrize(
    ("dsn", "schema", "lock_timeout", "message"),
    [
        ("", "agentbarrier", 30, "DSN"),
        ("dbname=unused\npassword=secret", "agentbarrier", 30, "control characters"),
        ("dbname=unused", "Invalid-Schema", 30, "schema"),
        ("dbname=unused", "agentbarrier", 0, "lock timeout"),
        ("dbname=unused", "agentbarrier", float("inf"), "lock timeout"),
        ("dbname=unused", "agentbarrier", 601, "ten minutes"),
    ],
)
def test_postgres_store_validates_configuration_before_connecting(
    dsn: str,
    schema: str,
    lock_timeout: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PostgresRuntimeStore(
            dsn,
            schema=schema,
            lock_timeout_seconds=lock_timeout,
        )


def test_postgres_store_requires_explicit_schema_creation(
    postgres_schema: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, schema = postgres_schema
    with pytest.raises(RuntimeStoreError, match="database migrate"):
        PostgresRuntimeStore(dsn, schema=schema)

    environment_name = "AGENTBARRIER_POSTGRES_MISSING_SCHEMA_TEST_DSN"
    monkeypatch.setenv(environment_name, dsn)
    with pytest.raises(SystemExit) as error:
        main(
            [
                "database",
                "status",
                "--postgres-dsn-env",
                environment_name,
                "--postgres-schema",
                schema,
            ]
        )
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "database migrate" in stderr
    assert dsn not in stderr

    with pytest.raises(ValueError, match="migrate=True"):
        PostgresRuntimeStore(dsn, schema=schema, create_schema=True)

    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        create_schema=True,
        migrate=True,
    ) as store:
        assert store.schema_version == "5"


def test_postgres_lock_timeout_rolls_back_and_connection_recovers(
    postgres_schema: tuple[str, str],
) -> None:
    import psycopg

    dsn, schema = postgres_schema
    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        create_schema=True,
        migrate=True,
    ):
        pass
    lock_key = int.from_bytes(
        sha256(f"agentbarrier-runtime:{schema}".encode("ascii")).digest()[:8],
        byteorder="big",
        signed=True,
    )

    with (
        psycopg.connect(dsn, autocommit=True) as lock_holder,
        PostgresRuntimeStore(dsn, schema=schema, lock_timeout_seconds=0.01) as store,
    ):
        lock_holder.execute("BEGIN")
        lock_holder.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        try:
            with pytest.raises(RuntimeStoreError, match="transaction lock"):
                store.set_pause(paused_by="test", reason="contention")
        finally:
            lock_holder.execute("ROLLBACK")

        pause = store.set_pause(paused_by="test", reason="connection recovered")
        assert pause.reason == "connection recovered"


def test_postgres_store_rejects_unknown_schema_version(
    postgres_schema: tuple[str, str],
) -> None:
    import psycopg
    from psycopg import sql

    dsn, schema = postgres_schema
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            ).format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("INSERT INTO {}.runtime_metadata VALUES ('schema_version', '99')").format(
                sql.Identifier(schema)
            )
        )

    with pytest.raises(RuntimeStoreError, match="requires migration"):
        PostgresRuntimeStore(dsn, schema=schema)
    with pytest.raises(RuntimeStoreError, match="unsupported PostgreSQL"):
        PostgresRuntimeStore(dsn, schema=schema, migrate=True)


def test_postgres_store_migrates_v1_and_fails_closed_for_legacy_execution(
    postgres_schema: tuple[str, str],
) -> None:
    import psycopg
    from psycopg import sql

    dsn, schema = postgres_schema
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema))
        )
        connection.execute(
            "CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '1')")
        connection.execute(
            """
            CREATE TABLE runtime_actions (
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
                result_json TEXT,
                error TEXT,
                decided_by TEXT,
                decision_reason TEXT,
                UNIQUE (namespace, tool_name, idempotency_key)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_actions (
                action_id, namespace, tool_name, arguments_json, idempotency_key,
                request_digest, policy_version, policy_rule, policy_effect, status,
                created_at_ns, updated_at_ns, expires_at_ns
            ) VALUES
                ('pending', 'n', 'tool', '{}', 'pending-key', 'pending-digest', '1',
                 'review', 'require_approval', 'pending', 1, 1, 11),
                ('legacy', 'n', 'tool', '{}', 'legacy-key', 'legacy-digest', '1',
                 'allow', 'allow', 'executing', 1, 1, NULL)
            """
        )

    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        migrate=True,
        clock_ns=Clock(),
    ) as store:
        assert store.schema_version == "5"
        legacy = store.get_action("legacy")
        assert legacy.status is RuntimeStatus.UNKNOWN
        assert legacy.error == "ExecutionLeaseExpired"
        pending = store.get_action("pending")
        assert pending.approval_ttl_ns == 10
        assert pending.organization_id == "default"
        assert pending.requested_by is None
        store.set_pause(paused_by="migration-test", reason="verify controls")
        store.configure_limit(
            "migration-limit",
            window_seconds=60,
            max_actions=1,
            updated_by="migration-test",
            reason="verify controls",
        )
        assert len(store.list_pauses()) == 1
        assert len(store.list_limits()) == 1
        assert store.verify_receipt_chain()
        assert store.verify_control_receipt_chain()


def test_postgres_cli_reads_dsn_from_environment_and_operates_controls(
    postgres_schema: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn, schema = postgres_schema
    environment_name = "AGENTBARRIER_POSTGRES_CLI_TEST_DSN"
    monkeypatch.setenv(environment_name, dsn)
    target = [
        "--postgres-dsn-env",
        environment_name,
        "--postgres-schema",
        schema,
    ]

    assert main(["database", "migrate", *target, "--postgres-create-schema"]) == 0
    migration_output = capsys.readouterr().out
    assert "schema version 5" in migration_output
    assert dsn not in migration_output

    assert (
        main(
            [
                "controls",
                "pause",
                *target,
                "--paused-by",
                "on-call",
                "--reason",
                "provider incident",
            ]
        )
        == 0
    )
    assert "paused namespace=* tool=*" in capsys.readouterr().out

    assert main(["controls", "status", *target, "--json"]) == 0
    status_output = capsys.readouterr().out
    status = json.loads(status_output)
    assert len(status["pauses"]) == 1
    assert status["control_chain_valid"] is True
    assert dsn not in status_output


def test_postgres_store_runs_dashboard_and_api_decisions(
    postgres_schema: tuple[str, str],
) -> None:
    pytest.importorskip("starlette", reason="service dependencies are not installed")
    from starlette.testclient import TestClient

    from agentbarrier.service import StaticBearerAuth, hash_bearer_token
    from agentbarrier.service.api import create_approval_app
    from agentbarrier.service.dashboard import create_dashboard_app

    token = "postgres-reviewer-token-012345678901"
    auth = StaticBearerAuth.from_mapping(
        {
            "version": "1",
            "tokens": [
                {
                    "subject": "postgres-reviewer",
                    "token_sha256": hash_bearer_token(token),
                    "scopes": ["actions:read", "actions:decide", "audit:read"],
                }
            ],
        }
    )
    dsn, schema = postgres_schema
    with PostgresRuntimeStore(
        dsn,
        schema=schema,
        create_schema=True,
        migrate=True,
    ) as store:
        for action_id in ("dashboard-action", "api-action"):
            request = RuntimeRequest(
                action_id=action_id,
                namespace="billing",
                tool_name="payments.refund",
                arguments={"request_id": action_id, "amount_cents": 2_500},
                idempotency_key=action_id,
                policy_version="postgres-service-v1",
                created_at_ns=1,
            )
            store.submit(
                request,
                PolicyDecision(
                    PolicyEffect.REQUIRE_APPROVAL,
                    "review refunds",
                    "postgres-service-v1",
                ),
            )

        dashboard = create_dashboard_app(
            store=store,
            auth=auth,
            cookie_secure=False,
            public_origin="http://testserver",
        )
        with TestClient(dashboard) as client:
            login = client.get("/dashboard/login")
            login_csrf = re.search(r'name="csrf" value="([A-Za-z0-9_-]+)"', login.text)
            assert login_csrf is not None
            signed_in = client.post(
                "/dashboard/login",
                data={"token": token, "csrf": login_csrf.group(1)},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            assert signed_in.status_code == 303
            detail = client.get("/dashboard/actions/dashboard-action")
            session_csrf = re.search(r'name="csrf" value="([A-Za-z0-9_-]+)"', detail.text)
            assert session_csrf is not None
            approved = client.post(
                "/dashboard/actions/dashboard-action/approve",
                data={"csrf": session_csrf.group(1)},
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
            assert approved.status_code == 303

        api = create_approval_app(store=store, auth=auth)
        with TestClient(api) as client:
            api_approved = client.post(
                "/v1/actions/api-action/approve",
                json={"reason": "postgres parity"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert api_approved.status_code == 200

        assert store.get_action("dashboard-action").decided_by == "postgres-reviewer"
        assert store.get_action("api-action").decided_by == "postgres-reviewer"
        assert store.verify_receipt_chain()
