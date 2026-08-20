"""Credential-free SQLite payment ledger used by the application-boundary example.

This is verification code, not production payment software. It deliberately keeps every effect
inside one local SQLite database so approval, retry, and reconciliation behavior is observable.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from agentbarrier.adapters.reference import ReferenceAdapter
from agentbarrier.journal import EffectJournal
from agentbarrier.models import (
    ActionRequest,
    ReconciliationEvidence,
    ReconciliationStatus,
    action_digest,
)
from agentbarrier.probe import EffectProbe
from examples.unsafe_approval import UnsafeApprovalAdapter


class PaymentConflictError(RuntimeError):
    """Raised when one operation identity is reused for different payment arguments."""


class PaymentLedger:
    """Small transactional ledger with an effect-boundary idempotency constraint."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0)
            );

            CREATE TABLE IF NOT EXISTS payment_operations (
                operation_id TEXT PRIMARY KEY,
                action_digest TEXT NOT NULL,
                source_account TEXT NOT NULL REFERENCES accounts(account_id),
                destination_account TEXT NOT NULL REFERENCES accounts(account_id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
            );
            """
        )

    def seed_account(self, account_id: str, balance_cents: int) -> None:
        """Create an example account before any payment runs."""

        if not account_id.strip():
            raise ValueError("account_id must not be empty")
        if balance_cents < 0:
            raise ValueError("balance_cents must not be negative")
        with self._lock:
            self._connection.execute(
                "INSERT INTO accounts (account_id, balance_cents) VALUES (?, ?)",
                (account_id, balance_cents),
            )

    def commit_payment(self, action: ActionRequest) -> bool:
        """Atomically transfer funds and persist the stable operation identity.

        Returns ``True`` for a new transfer and ``False`` for an identical replay.
        """

        source, destination, amount_cents = _payment_fields(action)
        digest = action_digest(action)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT action_digest FROM payment_operations WHERE operation_id = ?",
                    (action.action_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != digest:
                        raise PaymentConflictError(
                            f"operation {action.action_id!r} already exists with "
                            "different arguments"
                        )
                    self._connection.execute("COMMIT")
                    return False

                source_row = self._connection.execute(
                    "SELECT balance_cents FROM accounts WHERE account_id = ?",
                    (source,),
                ).fetchone()
                destination_row = self._connection.execute(
                    "SELECT balance_cents FROM accounts WHERE account_id = ?",
                    (destination,),
                ).fetchone()
                if source_row is None or destination_row is None:
                    raise ValueError("both payment accounts must exist")
                if int(source_row[0]) < amount_cents:
                    raise ValueError("insufficient example ledger balance")

                self._connection.execute(
                    "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id = ?",
                    (amount_cents, source),
                )
                self._connection.execute(
                    "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
                    (amount_cents, destination),
                )
                self._connection.execute(
                    """
                    INSERT INTO payment_operations (
                        operation_id, action_digest, source_account,
                        destination_account, amount_cents
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (action.action_id, digest, source, destination, amount_cents),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
        return True

    def reconcile_payment(self, action: ActionRequest) -> ReconciliationEvidence:
        """Read durable ledger state using the original operation identity."""

        expected_digest = action_digest(action)
        with self._lock:
            row = self._connection.execute(
                "SELECT action_digest FROM payment_operations WHERE operation_id = ?",
                (action.action_id,),
            ).fetchone()
        if row is None:
            return ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.NOT_COMMITTED,
                expected_action_digest=expected_digest,
                detail="the operation identity is absent from the payment ledger",
            )
        observed_digest = str(row[0])
        if observed_digest == expected_digest:
            return ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.COMMITTED,
                expected_action_digest=expected_digest,
                observed_action_digests=(observed_digest,),
                detail="the payment ledger contains one matching operation",
            )
        return ReconciliationEvidence(
            action_id=action.action_id,
            status=ReconciliationStatus.CONFLICT,
            expected_action_digest=expected_digest,
            observed_action_digests=(observed_digest,),
            detail="the operation identity exists with a different action digest",
        )

    def balance_cents(self, account_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT balance_cents FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(account_id)
        return int(row[0])

    def transaction_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM payment_operations").fetchone()
        if row is None:  # pragma: no cover - SQLite COUNT always returns one row
            raise RuntimeError("SQLite did not return a payment count")
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> PaymentLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SafePaymentAdapter(ReferenceAdapter):
    """Place the existing safe approval lifecycle before the injected ledger commit."""

    name = "sqlite-payment-ledger-safe"


class UnsafePaymentAdapter(UnsafeApprovalAdapter):
    """Intentionally schedule the injected ledger commit before approval."""

    name = "sqlite-payment-ledger-unsafe"


def payment_action(
    operation_id: str,
    *,
    amount_cents: int,
    requires_approval: bool = True,
) -> ActionRequest:
    """Build one stable payment proposal used throughout the example."""

    return ActionRequest(
        action_id=operation_id,
        tool_name="transfer_payment",
        arguments={
            "source_account": "customer",
            "destination_account": "merchant",
            "amount_cents": amount_cents,
        },
        requires_approval=requires_approval,
    )


def payment_probe(
    *,
    journal: EffectJournal,
    ledger: PaymentLedger,
    run_id: str,
    block_before_commit: bool = False,
    raise_after_commit: bool = False,
) -> EffectProbe:
    """Bind AgentBarrier's sentinel boundary to the local ledger transaction and lookup."""

    return EffectProbe(
        journal,
        run_id=run_id,
        block_before_commit=block_before_commit,
        raise_after_commit=raise_after_commit,
        commit_action=ledger.commit_payment,
        reconcile_action=ledger.reconcile_payment,
    )


def _payment_fields(action: ActionRequest) -> tuple[str, str, int]:
    if action.tool_name != "transfer_payment":
        raise ValueError("payment actions must use the transfer_payment tool")
    arguments = action.arguments
    source = arguments.get("source_account")
    destination = arguments.get("destination_account")
    amount_cents = arguments.get("amount_cents")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source_account must be a non-empty string")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination_account must be a non-empty string")
    if source == destination:
        raise ValueError("payment accounts must differ")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ValueError("amount_cents must be a positive integer")
    return source, destination, amount_cents
