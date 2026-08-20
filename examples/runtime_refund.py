"""Credential-free runtime approval example backed by a real SQLite effect."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from agentbarrier.errors import ApprovalRequired
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def issue_refund(
    request_id: str,
    account_id: str,
    amount: int,
    ledger_path: str,
) -> dict[str, object]:
    """Record one local refund; the wrapper ensures this effect is claimed once."""

    path = Path(ledger_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS refunds (
                request_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                amount INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO refunds (request_id, account_id, amount) VALUES (?, ?, ?)",
            (request_id, account_id, amount),
        )
    return {
        "request_id": request_id,
        "account_id": account_id,
        "amount": amount,
        "status": "refunded",
    }


def refund_policy() -> RuntimePolicy:
    """Allow small refunds, review larger ones, and deny extreme amounts."""

    return RuntimePolicy(
        version="refund-policy-v1",
        rules=(
            PolicyRule(
                "deny extreme refunds",
                PolicyEffect.DENY,
                tool="payments.refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 5_000),),
            ),
            PolicyRule(
                "review refunds over twenty",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
                approval_ttl_seconds=3_600,
            ),
            PolicyRule("allow small refunds", PolicyEffect.ALLOW, tool="payments.refund"),
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="AgentBarrier runtime SQLite database")
    parser.add_argument("--ledger", required=True, help="example refund ledger SQLite database")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--amount", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    with SQLiteRuntimeStore(arguments.db) as store:
        protected_refund = RuntimeBarrier(
            policy=refund_policy(),
            store=store,
            namespace="refund-example",
        ).protect(
            issue_refund,
            tool_name="payments.refund",
            idempotency_key="request_id",
        )
        try:
            result = protected_refund(
                arguments.request_id,
                arguments.account_id,
                arguments.amount,
                arguments.ledger,
            )
        except ApprovalRequired as exc:
            print(f"Approval required: {exc.action.action_id}")
            print(
                "Approve with: "
                f"agentbarrier approvals approve {exc.action.action_id} "
                f"--db {arguments.db} --decided-by REVIEWER"
            )
            return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
