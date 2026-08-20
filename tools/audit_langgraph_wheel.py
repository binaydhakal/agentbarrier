"""Audit an installed wheel through a real LangGraph tool lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolRuntime

from agentbarrier.errors import ApprovalRequired
from agentbarrier.integrations.langgraph import runtime_structured_tool, runtime_tool_node
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


def run_audit(directory: Path) -> dict[str, object]:
    """Run pending → approved → executed → replayed through a compiled graph."""

    async def exercise() -> dict[str, object]:
        directory.mkdir(parents=True, exist_ok=True)
        runtime_path = directory / "runtime.db"
        ledger_path = directory / "ledger.db"
        with sqlite3.connect(ledger_path) as ledger:
            ledger.execute("CREATE TABLE refunds (request_id TEXT PRIMARY KEY, amount INTEGER)")

        policy = RuntimePolicy(
            version="langgraph-wheel-audit-v1",
            rules=(
                PolicyRule(
                    "review refunds",
                    PolicyEffect.REQUIRE_APPROVAL,
                    tool="payments_refund",
                ),
            ),
        )

        with SQLiteRuntimeStore(runtime_path) as store:
            barrier = RuntimeBarrier(policy=policy, store=store, namespace="langgraph-wheel-audit")

            async def refund(
                request_id: str,
                amount: int,
                runtime: ToolRuntime,
            ) -> dict[str, object]:
                """Refund one exact payment request."""

                del runtime
                with sqlite3.connect(ledger_path) as ledger:
                    ledger.execute(
                        "INSERT INTO refunds (request_id, amount) VALUES (?, ?)",
                        (request_id, amount),
                    )
                return {"request_id": request_id, "amount": amount, "refunded": True}

            tool = runtime_structured_tool(
                refund,
                barrier=barrier,
                idempotency_key="request_id",
                name="payments_refund",
            )
            if set(tool.args) != {"request_id", "amount"}:
                raise AssertionError(f"unexpected model-visible tool schema: {tool.args}")

            builder = StateGraph(MessagesState)
            builder.add_node("tools", runtime_tool_node([tool]))
            builder.add_edge(START, "tools")
            builder.add_edge("tools", END)
            graph = builder.compile()

            def input_for(call_id: str) -> MessagesState:
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "payments_refund",
                            "args": {"request_id": "refund-wheel-1", "amount": 25},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
                return MessagesState(messages=[message])

            try:
                await graph.ainvoke(input_for("model-call-1"))
            except ApprovalRequired as error:
                action_id = error.action.action_id
            else:  # pragma: no cover - safety assertion
                raise AssertionError("LangGraph refund executed before approval")

            with sqlite3.connect(ledger_path) as ledger:
                if ledger.execute("SELECT COUNT(*) FROM refunds").fetchone() != (0,):
                    raise AssertionError("LangGraph refund effect committed before approval")

            store.decide(
                action_id,
                Decision.APPROVE,
                decided_by="wheel-audit",
                reason="clean-install LangGraph verification",
            )
            executed = await graph.ainvoke(input_for("model-call-2"))
            replayed = await graph.ainvoke(input_for("model-call-3"))
            expected = {"request_id": "refund-wheel-1", "amount": 25, "refunded": True}
            for result in (executed, replayed):
                if json.loads(result["messages"][-1].content) != expected:
                    raise AssertionError("LangGraph result or replay differs from expected output")

            with sqlite3.connect(ledger_path) as ledger:
                effect_count = int(ledger.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
            if effect_count != 1:
                raise AssertionError(f"expected one LangGraph effect, observed {effect_count}")
            events = [receipt.event.value for receipt in store.receipts(action_id=action_id)]
            expected_events = [
                "approval_requested",
                "approved",
                "execution_started",
                "execution_succeeded",
                "result_replayed",
            ]
            if events != expected_events:
                raise AssertionError(f"unexpected LangGraph runtime events: {events}")

        return {
            "action_id": action_id,
            "effect_count": effect_count,
            "events": events,
            "result": expected,
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
        with tempfile.TemporaryDirectory(prefix="agentbarrier-langgraph-wheel-audit-") as directory:
            result = run_audit(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
