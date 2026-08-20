"""Run the SQLite payment-ledger example without credentials or network access."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbarrier.journal import EffectJournal
from agentbarrier.models import RunStatus
from agentbarrier.reporters import render_console
from agentbarrier.runner import RunnerOptions, SuiteRunner
from examples.payment_ledger import (
    PaymentLedger,
    SafePaymentAdapter,
    UnsafePaymentAdapter,
    payment_action,
    payment_probe,
)


async def _run_real_payment(directory: Path) -> tuple[int, int, int]:
    with (
        PaymentLedger(directory / "payments.sqlite3") as ledger,
        EffectJournal(directory / "effects.sqlite3") as journal,
    ):
        ledger.seed_account("customer", 10_000)
        ledger.seed_account("merchant", 0)
        action = payment_action("demo:checkout-1001", amount_cents=2_500)
        probe = payment_probe(
            journal=journal,
            ledger=ledger,
            run_id="approved-payment",
        )
        handle = await SafePaymentAdapter().begin(
            run_id="approved-payment",
            actions=[action],
            effect=probe,
        )
        await handle.wait_for_pending(1)
        if ledger.transaction_count() != 0:
            raise AssertionError("the payment committed before approval")
        await handle.approve(action.action_id)
        outcome = await handle.wait(1)
        await handle.close()
        if outcome.status is not RunStatus.COMPLETED:
            raise AssertionError(f"approved payment ended as {outcome.status.value}")
        return (
            ledger.balance_cents("customer"),
            ledger.balance_cents("merchant"),
            ledger.transaction_count(),
        )


async def _run_real_unsafe_payment(directory: Path) -> tuple[int, int, int]:
    with (
        PaymentLedger(directory / "unsafe-payments.sqlite3") as ledger,
        EffectJournal(directory / "unsafe-effects.sqlite3") as journal,
    ):
        ledger.seed_account("customer", 10_000)
        ledger.seed_account("merchant", 0)
        action = payment_action("demo:unsafe-checkout", amount_cents=1_500)
        probe = payment_probe(
            journal=journal,
            ledger=ledger,
            run_id="unsafe-payment",
        )
        handle = await UnsafePaymentAdapter().begin(
            run_id="unsafe-payment",
            actions=[action],
            effect=probe,
        )
        await handle.wait_for_pending(1)
        await handle.wait(1)
        await handle.close()
        return (
            ledger.balance_cents("customer"),
            ledger.balance_cents("merchant"),
            ledger.transaction_count(),
        )


def main() -> int:
    unsafe = SuiteRunner(RunnerOptions(scenarios=("approval_hold",))).verify_sync(
        UnsafePaymentAdapter()
    )
    safe = SuiteRunner().verify_sync(SafePaymentAdapter())
    print("Unsafe boundary (expected failure):")
    print(render_console(unsafe, color=sys.stdout.isatty()))
    print("\nSafe boundary:")
    print(render_console(safe, color=sys.stdout.isatty()))
    with TemporaryDirectory(prefix="agentbarrier-payments-") as directory:
        path = Path(directory)
        unsafe_customer, unsafe_merchant, unsafe_count = asyncio.run(_run_real_unsafe_payment(path))
        customer, merchant, count = asyncio.run(_run_real_payment(path))
    print(
        "\nUnsafe local transfer before approval: "
        f"customer={unsafe_customer} cents, merchant={unsafe_merchant} cents, "
        f"transactions={unsafe_count}"
    )
    print(
        "\nApproved local transfer: "
        f"customer={customer} cents, merchant={merchant} cents, transactions={count}"
    )
    unsafe_finding = unsafe.results[0].finding
    return int(
        unsafe_finding is None
        or unsafe_finding.code != "AB002"
        or not safe.passed
        or (unsafe_customer, unsafe_merchant, unsafe_count) != (8_500, 1_500, 1)
        or (customer, merchant, count) != (7_500, 2_500, 1)
    )


if __name__ == "__main__":
    raise SystemExit(main())
