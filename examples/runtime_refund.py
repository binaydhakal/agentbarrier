"""Credential-free runtime approval example backed by a real SQLite effect."""

from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from agentbarrier.errors import ApprovalRequired
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeAction,
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


def _interactive_terminal_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _prompt_for_decision(
    action: RuntimeAction,
) -> tuple[Decision, str, str | None] | None:
    print()
    print("This exact refund requires your approval:")
    print(f"  account_id: {action.arguments['account_id']}")
    print(f"  amount: {action.arguments['amount']}")
    print(f"  request_id: {action.arguments['request_id']}")
    print(f"  action_id: {action.action_id}")
    print()
    while True:
        choice = _read_input("[a] Approve and execute  [r] Reject  [l] Leave pending: ").lower()
        decisions = {
            "a": Decision.APPROVE,
            "approve": Decision.APPROVE,
            "r": Decision.REJECT,
            "reject": Decision.REJECT,
        }
        decision = decisions.get(choice)
        if decision is not None:
            default_reviewer = getpass.getuser()
            reviewer = _read_input(f"Reviewer identity [{default_reviewer}]: ") or default_reviewer
            reason = _read_input("Reason (optional): ") or None
            return decision, reviewer, reason
        if choice in {"l", "leave", "later", "q", "quit", ""}:
            return None
        print("Choose a, r, or l.")


def _print_approval_command(action_id: str, database_path: str) -> None:
    print(
        "Review later with: "
        f"agentbarrier approvals review --db {database_path} --decided-by REVIEWER"
    )
    print(
        "Or approve directly with: "
        f"agentbarrier approvals approve {action_id} "
        f"--db {database_path} --decided-by REVIEWER"
    )


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
            review = _prompt_for_decision(exc.action) if _interactive_terminal_available() else None
            if review is None:
                _print_approval_command(exc.action.action_id, arguments.db)
                return 3
            decision, reviewer, reason = review
            store.decide(
                exc.action.action_id,
                decision,
                decided_by=reviewer,
                reason=reason,
            )
            if decision is Decision.REJECT:
                print(f"Rejected {exc.action.action_id}; the refund was not executed.")
                return 4
            print(f"Approved {exc.action.action_id}; executing the refund once.")
            result = protected_refund(
                arguments.request_id,
                arguments.account_id,
                arguments.amount,
                arguments.ledger,
            )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
