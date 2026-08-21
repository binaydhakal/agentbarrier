"""Audit an installed wheel through a real PydanticAI tool lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agentbarrier.errors import ApprovalRequired
from agentbarrier.integrations.pydantic_ai import runtime_tool
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed through a PydanticAI agent."""

    async def exercise() -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        runtime_path = directory / "runtime.db"
        ledger_path = directory / "ledger.db"
        with sqlite3.connect(ledger_path) as ledger:
            ledger.execute("CREATE TABLE refunds (request_id TEXT PRIMARY KEY, amount INTEGER)")

        policy = RuntimePolicy(
            version="pydantic-ai-wheel-audit-v1",
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
                namespace="pydantic-ai-wheel-audit",
            )

            async def refund(request_id: str, amount: int) -> dict[str, object]:
                """Refund one exact payment request."""

                with sqlite3.connect(ledger_path) as ledger:
                    ledger.execute(
                        "INSERT INTO refunds (request_id, amount) VALUES (?, ?)",
                        (request_id, amount),
                    )
                return {"request_id": request_id, "amount": amount, "refunded": True}

            tool = runtime_tool(
                refund,
                barrier=barrier,
                idempotency_key="request_id",
                name="payments_refund",
            )
            properties = tool.tool_def.parameters_json_schema.get("properties")
            if not isinstance(properties, dict) or set(properties) != {"request_id", "amount"}:
                raise AssertionError(f"unexpected model-visible tool schema: {properties}")

            active_call_id = "model-call-1"

            def respond(messages: list[Any], _info: Any) -> ModelResponse:
                if any(
                    isinstance(part, ToolReturnPart)
                    for message in messages
                    for part in getattr(message, "parts", ())
                ):
                    return ModelResponse(parts=[TextPart("done")])
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "payments_refund",
                            {"request_id": "refund-wheel-1", "amount": 25},
                            tool_call_id=active_call_id,
                        )
                    ]
                )

            model = FunctionModel(respond, model_name="agentbarrier-pydantic-wheel-audit")
            agent = Agent(model, tools=[tool])
            try:
                await agent.run("refund")
            except ApprovalRequired as error:
                action_id = error.action.action_id
            else:  # pragma: no cover - safety assertion
                raise AssertionError("PydanticAI refund executed before approval")

            with sqlite3.connect(ledger_path) as ledger:
                if ledger.execute("SELECT COUNT(*) FROM refunds").fetchone() != (0,):
                    raise AssertionError("PydanticAI refund effect committed before approval")

            store.decide(
                action_id,
                Decision.APPROVE,
                decided_by="wheel-audit",
                reason="clean-install PydanticAI verification",
            )
            active_call_id = "model-call-2"
            executed = await agent.run("refund")
            active_call_id = "model-call-3"
            replayed = await agent.run("refund")
            if executed.output != "done" or replayed.output != "done":
                raise AssertionError("PydanticAI agent did not receive successful tool results")

            with sqlite3.connect(ledger_path) as ledger:
                effect_count = int(ledger.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
            if effect_count != 1:
                raise AssertionError(f"expected one PydanticAI effect, observed {effect_count}")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            expected_events = [
                "approval_requested",
                "approved",
                "execution_started",
                "execution_succeeded",
                "result_replayed",
            ]
            if events != expected_events:
                raise AssertionError(f"unexpected PydanticAI runtime events: {events}")

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
        with tempfile.TemporaryDirectory(prefix="agentbarrier-pydantic-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
