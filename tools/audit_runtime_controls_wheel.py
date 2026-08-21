"""Audit installed-wheel emergency pause and atomic limit enforcement."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from agentbarrier.errors import ActionLimitExceeded, EmergencyPauseActive
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Prove pause, resume, one execution, and concurrent-safe budget blocking."""

    directory.mkdir(parents=True, exist_ok=True)
    runtime_path = directory / "runtime.db"
    ledger_path = directory / "ledger.db"
    with sqlite3.connect(ledger_path) as ledger:
        ledger.execute("CREATE TABLE effects (operation_id TEXT PRIMARY KEY, amount_cents INTEGER)")

    policy = RuntimePolicy(
        version="controls-wheel-audit-v1",
        rules=(PolicyRule("allow audited effect", PolicyEffect.ALLOW, tool="payments.refund"),),
    )
    with SQLiteRuntimeStore(runtime_path) as store:
        store.configure_limit(
            "one-refund-per-window",
            namespace="controls-wheel-audit",
            tool_name="payments.refund",
            window_seconds=60,
            max_actions=1,
            value_argument="amount_cents",
            max_value=5_000,
            updated_by="wheel-audit",
            reason="clean-install control verification",
        )
        store.set_pause(
            namespace="controls-wheel-audit",
            tool_name="payments.refund",
            paused_by="wheel-audit",
            reason="verify fail-closed pause",
        )
        barrier = RuntimeBarrier(
            policy=policy,
            store=store,
            namespace="controls-wheel-audit",
        )

        def refund(operation_id: str, amount_cents: int) -> dict[str, object]:
            with sqlite3.connect(ledger_path) as ledger:
                ledger.execute(
                    "INSERT INTO effects (operation_id, amount_cents) VALUES (?, ?)",
                    (operation_id, amount_cents),
                )
            return {"operation_id": operation_id, "status": "refunded"}

        protected = barrier.protect(
            refund,
            tool_name="payments.refund",
            idempotency_key="operation_id",
        )
        try:
            protected("refund-controls-1", 2_500)
        except EmergencyPauseActive:
            pass
        else:  # pragma: no cover - release safety assertion
            raise AssertionError("emergency pause did not block execution")

        with sqlite3.connect(ledger_path) as ledger:
            if ledger.execute("SELECT COUNT(*) FROM effects").fetchone() != (0,):
                raise AssertionError("effect committed while emergency pause was active")

        if not store.clear_pause(
            namespace="controls-wheel-audit",
            tool_name="payments.refund",
            resumed_by="wheel-audit",
            reason="continue clean-install control verification",
        ):
            raise AssertionError("expected emergency pause was not active")
        result = protected("refund-controls-1", 2_500)

        try:
            protected("refund-controls-2", 1_000)
        except ActionLimitExceeded as error:
            blocked_limit = error.limit_id
        else:  # pragma: no cover - release safety assertion
            raise AssertionError("atomic action limit did not block the second execution")

        with sqlite3.connect(ledger_path) as ledger:
            effect_count = int(ledger.execute("SELECT COUNT(*) FROM effects").fetchone()[0])
        if effect_count != 1:
            raise AssertionError(f"expected one effect, observed {effect_count}")
        usage = store.limit_usage("one-refund-per-window")[0]
        if usage.actions_used != 1 or usage.value_used != 2_500:
            raise AssertionError(f"unexpected limit usage: {usage}")
        if not store.verify_receipt_chain() or not store.verify_control_receipt_chain():
            raise AssertionError("runtime or control receipt chain is invalid")
        control_events = [receipt.event.value for receipt in store.control_receipts()]

    return {
        "blocked_limit": blocked_limit,
        "control_events": control_events,
        "effect_count": effect_count,
        "result": result,
        "status": "passed",
        "usage": {"actions": usage.actions_used, "value": usage.value_used},
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(options.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="agentbarrier-controls-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
