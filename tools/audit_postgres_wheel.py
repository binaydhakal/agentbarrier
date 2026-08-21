"""Audit an installed wheel through PostgreSQL lifecycle and control invariants."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

from agentbarrier.errors import ActionLimitExceeded, ApprovalRequired, EmergencyPauseActive
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    PostgresRuntimeStore,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
)


def run_audit(*, dsn_environment: str, directory: Path) -> dict[str, object]:
    """Prove approval, replay, pause, limits, and receipt chains in an installed wheel."""

    dsn = os.environ.get(dsn_environment)
    if dsn is None:
        raise ValueError(f"environment variable {dsn_environment!r} is not set")
    directory.mkdir(parents=True, exist_ok=True)
    schema = f"agentbarrier_audit_{uuid.uuid4().hex}"
    effects: list[str] = []
    policy = RuntimePolicy(
        version="postgres-wheel-audit-v1",
        rules=(
            PolicyRule(
                "review refunds",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
                approval_ttl_seconds=60,
            ),
        ),
    )

    try:
        with PostgresRuntimeStore(
            dsn,
            schema=schema,
            create_schema=True,
            migrate=True,
        ) as store:
            store.configure_limit(
                "one-refund",
                namespace="postgres-wheel-audit",
                tool_name="payments.refund",
                window_seconds=60,
                max_actions=1,
                updated_by="wheel-audit",
                reason="clean-install concurrency boundary",
            )
            barrier = RuntimeBarrier(
                policy=policy,
                store=store,
                namespace="postgres-wheel-audit",
            )

            def refund(request_id: str, amount_cents: int) -> dict[str, object]:
                effects.append(request_id)
                return {"request_id": request_id, "amount_cents": amount_cents, "refunded": True}

            protected = barrier.protect(
                refund,
                tool_name="payments.refund",
                idempotency_key="request_id",
            )
            arguments = ("postgres-wheel-refund-1", 2_500)
            try:
                protected(*arguments)
            except ApprovalRequired as error:
                action_id = error.action.action_id
            else:  # pragma: no cover - safety assertion
                raise AssertionError("PostgreSQL action executed before approval")

            store.decide(
                action_id,
                Decision.APPROVE,
                decided_by="postgres-wheel-reviewer",
                reason="clean-install verification",
            )
            store.set_pause(
                namespace="postgres-wheel-audit",
                tool_name="payments.refund",
                paused_by="postgres-wheel-operator",
                reason="pause verification",
            )
            try:
                protected(*arguments)
            except EmergencyPauseActive:
                pass
            else:  # pragma: no cover - safety assertion
                raise AssertionError("PostgreSQL pause did not block the approved action")
            store.clear_pause(
                namespace="postgres-wheel-audit",
                tool_name="payments.refund",
                resumed_by="postgres-wheel-operator",
                reason="resume verification",
            )

            executed = protected(*arguments)
            replayed = protected(*arguments)
            if executed != replayed or effects != [arguments[0]]:
                raise AssertionError("PostgreSQL replay did not preserve exactly-once execution")

            try:
                protected("postgres-wheel-refund-2", 1_000)
            except ApprovalRequired as error:
                second_action_id = error.action.action_id
            else:  # pragma: no cover - safety assertion
                raise AssertionError("second PostgreSQL action did not require approval")
            store.decide(second_action_id, Decision.APPROVE, decided_by="postgres-wheel-reviewer")
            try:
                protected("postgres-wheel-refund-2", 1_000)
            except ActionLimitExceeded:
                pass
            else:  # pragma: no cover - safety assertion
                raise AssertionError("PostgreSQL atomic action limit did not block the second call")

            action = store.get_action(action_id)
            if action.status is not RuntimeStatus.SUCCEEDED:
                raise AssertionError(f"expected succeeded action, observed {action.status.value}")
            if not store.verify_receipt_chain() or not store.verify_control_receipt_chain():
                raise AssertionError("PostgreSQL receipt chain verification failed")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            control_events = [receipt.event.value for receipt in store.control_receipts()]
            usage = store.limit_usage("one-refund")[0]
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

    return {
        "action_id": action_id,
        "control_events": control_events,
        "effect_count": len(effects),
        "events": events,
        "status": "passed",
        "usage": {"actions": usage.actions_used, "value": usage.value_used},
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn-env",
        default="AGENTBARRIER_TEST_POSTGRES_DSN",
        help="environment variable containing the PostgreSQL DSN",
    )
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(
            dsn_environment=options.dsn_env,
            directory=options.directory,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="agentbarrier-postgres-wheel-audit-") as directory:
            result = run_audit(
                dsn_environment=options.dsn_env,
                directory=Path(directory),
            )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
