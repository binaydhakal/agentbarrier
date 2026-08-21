"""Audit an installed wheel through a real OpenAI Agents FunctionTool lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from agents import RunContextWrapper
from agents.tool_context import ToolContext

from agentbarrier.errors import ApprovalRequired
from agentbarrier.integrations.openai_agents import runtime_function_tool
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed through a real FunctionTool."""

    async def exercise() -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        ledger_path = directory / "ledger.db"
        with sqlite3.connect(ledger_path) as ledger:
            ledger.execute("CREATE TABLE refunds (request_id TEXT PRIMARY KEY, amount INTEGER)")

        policy = RuntimePolicy(
            version="openai-agents-wheel-v1",
            rules=(
                PolicyRule(
                    "review refunds",
                    PolicyEffect.REQUIRE_APPROVAL,
                    tool="payments_refund",
                ),
            ),
        )
        with SQLiteRuntimeStore(directory / "runtime.db") as store:
            barrier = RuntimeBarrier(
                policy=policy,
                store=store,
                namespace="openai-agents-wheel-audit",
            )

            async def refund(
                context: RunContextWrapper[dict[str, str]],
                request_id: str,
                amount: int,
            ) -> dict[str, object]:
                with sqlite3.connect(ledger_path) as ledger:
                    ledger.execute(
                        "INSERT INTO refunds (request_id, amount) VALUES (?, ?)",
                        (request_id, amount),
                    )
                return {
                    "request_id": request_id,
                    "amount": amount,
                    "tenant": context.context["tenant"],
                    "refunded": True,
                }

            tool = runtime_function_tool(
                refund,
                barrier=barrier,
                idempotency_key="request_id",
                name_override="payments_refund",
                description_override="Refund one exact payment request.",
            )
            if set(tool.params_json_schema["properties"]) != {"request_id", "amount"}:
                raise AssertionError("installed OpenAI tool exposed injected context")
            arguments = json.dumps({"request_id": "refund-wheel-1", "amount": 25})

            def context(call_id: str) -> ToolContext[dict[str, str]]:
                return ToolContext(
                    context={"tenant": "acme"},
                    tool_name=tool.name,
                    tool_call_id=call_id,
                    tool_arguments=arguments,
                )

            try:
                await tool.on_invoke_tool(context("model-call-1"), arguments)
            except ApprovalRequired as error:
                action_id = error.action.action_id
            else:  # pragma: no cover - audit assertion
                raise AssertionError("OpenAI Agents refund executed before approval")

            with sqlite3.connect(ledger_path) as ledger:
                if ledger.execute("SELECT COUNT(*) FROM refunds").fetchone() != (0,):
                    raise AssertionError("OpenAI Agents effect committed before approval")
            store.decide(
                action_id,
                Decision.APPROVE,
                decided_by="wheel-audit",
                reason="clean-install OpenAI Agents verification",
            )
            executed = await tool.on_invoke_tool(context("model-call-2"), arguments)
            replayed = await tool.on_invoke_tool(context("model-call-3"), arguments)
            if executed != replayed:
                raise AssertionError("OpenAI Agents replay did not return the durable result")
            with sqlite3.connect(ledger_path) as ledger:
                effect_count = int(ledger.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
            if effect_count != 1:
                raise AssertionError(f"expected one OpenAI effect, observed {effect_count}")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            expected_events = [
                "approval_requested",
                "approved",
                "execution_started",
                "execution_succeeded",
                "result_replayed",
            ]
            if events != expected_events:
                raise AssertionError(f"unexpected OpenAI Agents runtime events: {events}")

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
            prefix="agentbarrier-openai-agents-wheel-audit-"
        ) as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
