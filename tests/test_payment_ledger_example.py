from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentbarrier.journal import EffectJournal
from agentbarrier.models import AuditEvent, ReconciliationStatus, RunStatus
from agentbarrier.runner import RunnerOptions, SuiteRunner
from examples.payment_ledger import (
    PaymentConflictError,
    PaymentLedger,
    SafePaymentAdapter,
    UnsafePaymentAdapter,
    payment_action,
    payment_probe,
)


def _seeded_ledger(path: Path) -> PaymentLedger:
    ledger = PaymentLedger(path)
    ledger.seed_account("customer", 10_000)
    ledger.seed_account("merchant", 0)
    return ledger


def test_payment_adapters_expose_the_unsafe_and_safe_boundaries(tmp_path: Path) -> None:
    unsafe_suite = SuiteRunner(RunnerOptions(scenarios=("approval_hold",))).verify_sync(
        UnsafePaymentAdapter()
    )
    safe_suite = SuiteRunner().verify_sync(SafePaymentAdapter())

    assert not unsafe_suite.passed
    assert unsafe_suite.results[0].finding is not None
    assert unsafe_suite.results[0].finding.code == "AB002"
    assert safe_suite.passed
    assert safe_suite.passed_count == 11

    async def exercise_real_unsafe_boundary() -> None:
        with (
            _seeded_ledger(tmp_path / "unsafe-payments.sqlite3") as ledger,
            EffectJournal(tmp_path / "unsafe-effects.sqlite3") as journal,
        ):
            action = payment_action("unsafe:checkout", amount_cents=1_500)
            probe = payment_probe(journal=journal, ledger=ledger, run_id="unsafe-run")
            handle = await UnsafePaymentAdapter().begin(
                run_id="unsafe-run",
                actions=[action],
                effect=probe,
            )
            await handle.wait_for_pending(1)
            await handle.wait(1)

            assert ledger.balance_cents("customer") == 8_500
            assert ledger.balance_cents("merchant") == 1_500
            assert ledger.transaction_count() == 1
            await handle.close()

    asyncio.run(exercise_real_unsafe_boundary())


def test_safe_payment_approval_rejection_and_replay_preserve_ledger_state(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        adapter = SafePaymentAdapter()
        with (
            _seeded_ledger(tmp_path / "safe-payments.sqlite3") as ledger,
            EffectJournal(tmp_path / "safe-effects.sqlite3") as journal,
        ):
            approved = payment_action("safe:approved", amount_cents=2_500)
            approved_probe = payment_probe(
                journal=journal,
                ledger=ledger,
                run_id="approved-run",
            )
            approved_handle = await adapter.begin(
                run_id="approved-run",
                actions=[approved],
                effect=approved_probe,
            )
            await approved_handle.wait_for_pending(1)
            assert ledger.balance_cents("customer") == 10_000
            assert ledger.balance_cents("merchant") == 0
            assert ledger.transaction_count() == 0

            await approved_handle.approve(approved.action_id)
            assert (await approved_handle.wait(1)).status is RunStatus.COMPLETED
            replay = await approved_handle.replay()
            await replay.wait_for_pending(1)
            assert ledger.transaction_count() == 1
            await replay.approve(approved.action_id)
            assert (await replay.wait(1)).status is RunStatus.COMPLETED

            rejected = payment_action("safe:rejected", amount_cents=1_000)
            rejected_probe = payment_probe(
                journal=journal,
                ledger=ledger,
                run_id="rejected-run",
            )
            rejected_handle = await adapter.begin(
                run_id="rejected-run",
                actions=[rejected],
                effect=rejected_probe,
            )
            await rejected_handle.wait_for_pending(1)
            await rejected_handle.reject(rejected.action_id, "customer declined")
            assert (await rejected_handle.wait(1)).status is RunStatus.COMPLETED

            assert ledger.balance_cents("customer") == 7_500
            assert ledger.balance_cents("merchant") == 2_500
            assert ledger.transaction_count() == 1
            await rejected_handle.close()
            await replay.close()
            await approved_handle.close()

    asyncio.run(exercise())


def test_payment_response_loss_reconciles_against_the_ledger_without_duplication(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        adapter = SafePaymentAdapter()
        with (
            _seeded_ledger(tmp_path / "unknown-payments.sqlite3") as ledger,
            EffectJournal(tmp_path / "unknown-effects.sqlite3") as journal,
        ):
            action = payment_action(
                "safe:response-loss",
                amount_cents=3_000,
                requires_approval=False,
            )
            probe = payment_probe(
                journal=journal,
                ledger=ledger,
                run_id="response-loss-run",
                raise_after_commit=True,
            )
            handle = await adapter.begin(
                run_id="response-loss-run",
                actions=[action],
                effect=probe,
            )
            assert (await handle.wait(1)).status is RunStatus.UNKNOWN
            assert ledger.balance_cents("customer") == 7_000
            assert ledger.balance_cents("merchant") == 3_000
            assert ledger.transaction_count() == 1

            evidence = await handle.reconcile(action.action_id, 0.1)
            assert evidence.status is ReconciliationStatus.COMMITTED
            replay = await handle.replay()
            assert (await replay.wait(1)).status is RunStatus.COMPLETED
            assert ledger.balance_cents("customer") == 7_000
            assert ledger.balance_cents("merchant") == 3_000
            assert ledger.transaction_count() == 1
            receipts = await handle.audit_receipts()
            assert any(
                receipt.event is AuditEvent.RECONCILIATION_COMMITTED
                and receipt.action_id == action.action_id
                for receipt in receipts
            )
            await replay.close()
            await handle.close()

    asyncio.run(exercise())


def test_payment_cancellation_and_timeout_leave_no_database_effect(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = SafePaymentAdapter()
        with (
            _seeded_ledger(tmp_path / "fenced-payments.sqlite3") as ledger,
            EffectJournal(tmp_path / "fenced-effects.sqlite3") as journal,
        ):
            cancelled = payment_action(
                "safe:cancelled",
                amount_cents=1_000,
                requires_approval=False,
            )
            cancelled_probe = payment_probe(
                journal=journal,
                ledger=ledger,
                run_id="cancelled-run",
                block_before_commit=True,
            )
            cancelled_handle = await adapter.begin(
                run_id="cancelled-run",
                actions=[cancelled],
                effect=cancelled_probe,
            )
            await cancelled_probe.wait_started(1)
            await cancelled_handle.cancel()
            cancelled_probe.release()
            assert (await cancelled_handle.wait(1)).status is RunStatus.CANCELLED

            timed_out = payment_action(
                "safe:timed-out",
                amount_cents=1_000,
                requires_approval=False,
            )
            timed_out_probe = payment_probe(
                journal=journal,
                ledger=ledger,
                run_id="timed-out-run",
                block_before_commit=True,
            )
            timed_out_handle = await adapter.begin(
                run_id="timed-out-run",
                actions=[timed_out],
                effect=timed_out_probe,
                timeout_seconds=0.01,
            )
            await timed_out_probe.wait_started(1)
            assert (await timed_out_handle.wait(1)).status is RunStatus.TIMED_OUT
            timed_out_probe.release()
            await asyncio.sleep(0.02)

            assert ledger.balance_cents("customer") == 10_000
            assert ledger.balance_cents("merchant") == 0
            assert ledger.transaction_count() == 0
            await timed_out_handle.close()
            await cancelled_handle.close()

    asyncio.run(exercise())


def test_payment_operation_identity_deduplicates_and_surfaces_conflicts(tmp_path: Path) -> None:
    with _seeded_ledger(tmp_path / "identity-payments.sqlite3") as ledger:
        original = payment_action(
            "stable:checkout-42",
            amount_cents=2_000,
            requires_approval=False,
        )
        conflicting = payment_action(
            "stable:checkout-42",
            amount_cents=2_001,
            requires_approval=False,
        )

        assert ledger.commit_payment(original) is True
        assert ledger.commit_payment(original) is False
        evidence = ledger.reconcile_payment(conflicting)

        assert evidence.status is ReconciliationStatus.CONFLICT
        with pytest.raises(PaymentConflictError, match="different arguments"):
            ledger.commit_payment(conflicting)
        assert ledger.balance_cents("customer") == 8_000
        assert ledger.balance_cents("merchant") == 2_000
        assert ledger.transaction_count() == 1
