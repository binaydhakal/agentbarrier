"""Audit an installed wheel through the complete runtime approval lifecycle."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from agentbarrier.errors import ApprovalRequired
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed against a real SQLite effect."""

    directory.mkdir(parents=True, exist_ok=True)
    runtime_path = directory / "runtime.db"
    ledger_path = directory / "ledger.db"
    with sqlite3.connect(ledger_path) as ledger:
        ledger.execute(
            "CREATE TABLE refunds (request_id TEXT PRIMARY KEY, account_id TEXT, amount INTEGER)"
        )

    policy = RuntimePolicy(
        version="wheel-audit-v1",
        rules=(
            PolicyRule(
                "review refunds",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 0),),
                approval_ttl_seconds=60,
            ),
        ),
    )

    with SQLiteRuntimeStore(runtime_path) as store:
        barrier = RuntimeBarrier(policy=policy, store=store, namespace="wheel-audit")

        def refund(request_id: str, account_id: str, amount: int) -> dict[str, object]:
            with sqlite3.connect(ledger_path) as ledger:
                ledger.execute(
                    "INSERT INTO refunds (request_id, account_id, amount) VALUES (?, ?, ?)",
                    (request_id, account_id, amount),
                )
            return {"request_id": request_id, "status": "refunded", "amount": amount}

        protected_refund = barrier.protect(
            refund,
            tool_name="payments.refund",
            idempotency_key="request_id",
        )
        arguments = ("refund-wheel-1", "account-wheel-1", 25)

        try:
            protected_refund(*arguments)
        except ApprovalRequired as error:
            action_id = error.action.action_id
            if error.action.status is not RuntimeStatus.PENDING:
                raise AssertionError("first call did not create a pending action") from error
        else:  # pragma: no cover - safety assertion
            raise AssertionError("protected refund executed before approval")

        with sqlite3.connect(ledger_path) as ledger:
            if ledger.execute("SELECT COUNT(*) FROM refunds").fetchone() != (0,):
                raise AssertionError("refund effect committed before approval")

        store.decide(
            action_id,
            Decision.APPROVE,
            decided_by="wheel-audit",
            reason="clean-install release verification",
        )
        executed = protected_refund(*arguments)
        replayed = protected_refund(*arguments)
        if executed != replayed:
            raise AssertionError("replayed result differs from the executed result")

        with sqlite3.connect(ledger_path) as ledger:
            effect_count = int(ledger.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
        if effect_count != 1:
            raise AssertionError(f"expected one refund effect, observed {effect_count}")

        action = store.get_action(action_id)
        if action.status is not RuntimeStatus.SUCCEEDED:
            raise AssertionError(f"expected succeeded action, observed {action.status.value}")
        if not store.verify_receipt_chain():
            raise AssertionError("runtime receipt chain is invalid")
        events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
        expected_events = [
            "approval_requested",
            "approved",
            "execution_started",
            "execution_succeeded",
            "result_replayed",
        ]
        if events != expected_events:
            raise AssertionError(f"unexpected runtime events: {events}")

    return {
        "action_id": action_id,
        "effect_count": effect_count,
        "events": events,
        "result": replayed,
        "status": "passed",
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(options.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="agentbarrier-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
