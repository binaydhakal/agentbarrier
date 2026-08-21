"""Audit migration, backup/restore, replay, and downgrade refusal from an installed wheel."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from agentbarrier.errors import RuntimeStoreError
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)


class Clock:
    def __init__(self, value: int = 100) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 1
        return self.value


def create_v1_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
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
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                expires_at_ns INTEGER,
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
            ) VALUES (
                'legacy-pending', 'legacy', 'audit.review', '{}', 'pending-key',
                'pending-digest', 'legacy-v1', 'review', 'require_approval', 'pending',
                1, 1, 1000000
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_actions (
                action_id, namespace, tool_name, arguments_json, idempotency_key,
                request_digest, policy_version, policy_rule, policy_effect, status,
                created_at_ns, updated_at_ns
            ) VALUES (
                'legacy-executing', 'legacy', 'audit.execute', '{}', 'executing-key',
                'executing-digest', 'legacy-v1', 'allow', 'allow', 'executing', 2, 2
            )
            """
        )


def create_future_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '999')")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentbarrier-recovery-audit-") as temporary:
        directory = Path(temporary)
        live_path = directory / "runtime.db"
        backup_path = directory / "runtime.backup.db"
        restored_path = directory / "runtime.restored.db"
        future_path = directory / "runtime.future.db"
        create_v1_database(live_path)

        clock = Clock()
        executions: list[str] = []
        policy = RuntimePolicy(
            version="recovery-audit-v1",
            rules=(PolicyRule("allow-audit", PolicyEffect.ALLOW, tool="audit.echo"),),
        )
        with SQLiteRuntimeStore(live_path, clock_ns=clock) as store:
            assert store.schema_version == "5"
            legacy_pending = store.get_action("legacy-pending")
            assert legacy_pending.status is RuntimeStatus.PENDING
            assert legacy_pending.approval_ttl_ns == 999_999
            assert legacy_pending.organization_id == "default"
            assert legacy_pending.requested_by is None
            legacy_executing = store.get_action("legacy-executing")
            assert legacy_executing.status is RuntimeStatus.UNKNOWN
            assert legacy_executing.error == "ExecutionLeaseExpired"

            barrier = RuntimeBarrier(policy=policy, store=store, namespace="audit")

            def echo_operation(request_id: str, value: str) -> dict[str, str]:
                executions.append(request_id)
                return {"request_id": request_id, "value": value}

            echo = barrier.protect(
                echo_operation,
                tool_name="audit.echo",
                idempotency_key="request_id",
            )
            expected = {"request_id": "restore-1", "value": "durable"}
            assert echo("restore-1", "durable") == expected
            assert echo("restore-1", "durable") == expected
            assert executions == ["restore-1"]
            store.set_pause(paused_by="recovery-audit", reason="backup drill")
            store.clear_pause(resumed_by="recovery-audit", reason="backup ready")
            assert store.verify_receipt_chain()
            assert store.verify_control_receipt_chain()
            assert store.backup(backup_path) == backup_path

        assert backup_path.stat().st_mode & 0o777 == 0o600
        shutil.copy2(backup_path, restored_path)
        replay_executions: list[str] = []
        with SQLiteRuntimeStore(restored_path, clock_ns=clock) as restored:
            assert restored.schema_version == "5"
            assert restored.verify_receipt_chain()
            assert restored.verify_control_receipt_chain()
            assert restored.get_action("legacy-executing").status is RuntimeStatus.UNKNOWN
            barrier = RuntimeBarrier(policy=policy, store=restored, namespace="audit")

            def replay_operation(request_id: str, value: str) -> dict[str, str]:
                replay_executions.append(request_id)
                return {"request_id": request_id, "value": value}

            replay = barrier.protect(
                replay_operation,
                tool_name="audit.echo",
                idempotency_key="request_id",
            )
            assert replay("restore-1", "durable") == {
                "request_id": "restore-1",
                "value": "durable",
            }
            assert replay_executions == []

        create_future_database(future_path)
        try:
            SQLiteRuntimeStore(future_path)
        except RuntimeStoreError as error:
            assert "unsupported runtime schema version" in str(error)
        else:  # pragma: no cover - audit assertion
            raise AssertionError("newer runtime schema did not fail closed")

        print(
            json.dumps(
                {
                    "backup_mode": oct(backup_path.stat().st_mode & 0o777),
                    "downgrade_refused": True,
                    "legacy_execution": "unknown",
                    "replay_executions": len(replay_executions),
                    "restored_schema": "5",
                    "status": "passed",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
