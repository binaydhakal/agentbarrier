"""Audit an installed wheel through a real Google ADK FunctionTool lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from google.adk.sessions.session import Session
from google.adk.tools import ToolContext

from agentbarrier.errors import ApprovalRequired
from agentbarrier.integrations.google_adk import runtime_function_tool
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def _tool_context(function_call_id: str) -> ToolContext:
    session = Session(id="session", appName="agentbarrier", userId="wheel-audit")
    invocation_context = SimpleNamespace(session=session)
    return ToolContext(cast(Any, invocation_context), function_call_id=function_call_id)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed through a Google ADK FunctionTool."""

    async def exercise() -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        runtime_path = directory / "runtime.db"
        ledger_path = directory / "ledger.db"
        with sqlite3.connect(ledger_path) as ledger:
            ledger.execute("CREATE TABLE refunds (request_id TEXT PRIMARY KEY, amount INTEGER)")

        policy = RuntimePolicy(
            version="google-adk-wheel-audit-v1",
            rules=(
                PolicyRule(
                    "review refunds",
                    PolicyEffect.REQUIRE_APPROVAL,
                    tool="payments_refund",
                ),
            ),
        )

        with SQLiteRuntimeStore(runtime_path) as store:
            barrier = RuntimeBarrier(
                policy=policy,
                store=store,
                namespace="google-adk-wheel-audit",
            )

            async def refund(request_id: str, amount: int) -> dict[str, object]:
                """Refund one exact payment request."""

                with sqlite3.connect(ledger_path) as ledger:
                    ledger.execute(
                        "INSERT INTO refunds (request_id, amount) VALUES (?, ?)",
                        (request_id, amount),
                    )
                return {"request_id": request_id, "amount": amount, "refunded": True}

            tool = runtime_function_tool(
                refund,
                barrier=barrier,
                idempotency_key="request_id",
                name="payments_refund",
            )
            arguments = {"request_id": "refund-wheel-1", "amount": 25}
            try:
                await tool.run_async(
                    args=arguments,
                    tool_context=_tool_context("model-call-1"),
                )
            except ApprovalRequired as error:
                action_id = error.action.action_id
            else:  # pragma: no cover - safety assertion
                raise AssertionError("Google ADK refund executed before approval")

            with sqlite3.connect(ledger_path) as ledger:
                if ledger.execute("SELECT COUNT(*) FROM refunds").fetchone() != (0,):
                    raise AssertionError("Google ADK refund effect committed before approval")

            store.decide(
                action_id,
                Decision.APPROVE,
                decided_by="wheel-audit",
                reason="clean-install Google ADK verification",
            )
            executed = await tool.run_async(
                args=arguments,
                tool_context=_tool_context("model-call-2"),
            )
            replayed = await tool.run_async(
                args=arguments,
                tool_context=_tool_context("model-call-3"),
            )
            if executed != replayed:
                raise AssertionError("Google ADK replay did not return the durable result")

            with sqlite3.connect(ledger_path) as ledger:
                effect_count = int(ledger.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
            if effect_count != 1:
                raise AssertionError(f"expected one Google ADK effect, observed {effect_count}")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            expected_events = [
                "approval_requested",
                "approved",
                "execution_started",
                "execution_succeeded",
                "result_replayed",
            ]
            if events != expected_events:
                raise AssertionError(f"unexpected Google ADK runtime events: {events}")

        return {
            "action_id": action_id,
            "effect_count": effect_count,
            "events": events,
            "status": "passed",
        }

    return asyncio.run(exercise())


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args(arguments)
    if options.directory is not None:
        result = run_audit(options.directory)
    else:
        with tempfile.TemporaryDirectory(
            prefix="agentbarrier-google-adk-wheel-audit-"
        ) as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
