from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentbarrier.errors import ActionBindingError, ApprovalRequired
from agentbarrier.integrations.langgraph import runtime_structured_tool, runtime_tool_node
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimeEvent,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)

langchain_messages = pytest.importorskip(
    "langchain_core.messages", reason="LangGraph optional dependency is not installed"
)
langgraph_graph = pytest.importorskip(
    "langgraph.graph", reason="LangGraph optional dependency is not installed"
)
langgraph_prebuilt = pytest.importorskip(
    "langgraph.prebuilt", reason="LangGraph optional dependency is not installed"
)
AIMessage = langchain_messages.AIMessage
END = langgraph_graph.END
START = langgraph_graph.START
MessagesState = langgraph_graph.MessagesState
StateGraph = langgraph_graph.StateGraph
ToolRuntime = langgraph_prebuilt.ToolRuntime


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        version="langgraph-runtime-v1",
        rules=(
            PolicyRule(
                "review refunds",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments_refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
            ),
            PolicyRule("allow small refunds", PolicyEffect.ALLOW, tool="payments_refund"),
        ),
    )


def tool_call(call_id: str, *, request_id: str, amount: int) -> dict[str, object]:
    return {
        "name": "payments_refund",
        "args": {"request_id": request_id, "amount": amount},
        "id": call_id,
        "type": "tool_call",
    }


def compile_tool_graph(tool: object) -> object:
    builder = StateGraph(MessagesState)
    builder.add_node("tools", runtime_tool_node([tool]))  # type: ignore[list-item]
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


def test_langgraph_runtime_tool_holds_runtime_aware_effect_then_replays(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, int, str]] = []

        async def refund(
            request_id: str,
            amount: int,
            runtime: ToolRuntime,
        ) -> dict[str, object]:
            calls.append((request_id, amount, runtime.tool_call_id))
            return {"request_id": request_id, "amount": amount, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_structured_tool(
                refund,
                barrier=RuntimeBarrier(
                    policy=make_policy(),
                    store=store,
                    namespace="langgraph",
                ),
                idempotency_key="request_id",
                name="payments_refund",
                description="Refund one exact payment request.",
            )
            assert tool.name == "payments_refund"
            assert tool.description == "Refund one exact payment request."
            assert set(tool.args) == {"request_id", "amount"}
            graph = compile_tool_graph(tool)

            first = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-1", request_id="refund-42", amount=100)],
            )
            with pytest.raises(ApprovalRequired) as pending:
                await graph.ainvoke({"messages": [first]})
            assert calls == []
            assert pending.value.action.arguments == {"request_id": "refund-42", "amount": 100}
            assert "runtime" not in pending.value.action.arguments
            store.decide(
                pending.value.action.action_id,
                Decision.APPROVE,
                decided_by="finance-reviewer",
            )

            second = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-2", request_id="refund-42", amount=100)],
            )
            output = await graph.ainvoke({"messages": [second]})
            expected = {"request_id": "refund-42", "amount": 100, "refunded": True}
            assert json.loads(output["messages"][-1].content) == expected
            third = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-3", request_id="refund-42", amount=100)],
            )
            replay = await graph.ainvoke({"messages": [third]})
            assert json.loads(replay["messages"][-1].content) == expected
            assert calls == [("refund-42", 100, "model-call-2")]
            assert [receipt.event for receipt in store.receipts()] == [
                RuntimeEvent.APPROVAL_REQUESTED,
                RuntimeEvent.APPROVED,
                RuntimeEvent.EXECUTION_STARTED,
                RuntimeEvent.EXECUTION_SUCCEEDED,
                RuntimeEvent.RESULT_REPLAYED,
            ]

            changed = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-4", request_id="refund-42", amount=101)],
            )
            with pytest.raises(ActionBindingError):
                await graph.ainvoke({"messages": [changed]})

    asyncio.run(run())


def test_langgraph_runtime_node_propagates_post_claim_failure(tmp_path: Path) -> None:
    async def run() -> None:
        async def refund(
            request_id: str,
            amount: int,
            runtime: ToolRuntime,
        ) -> dict[str, object]:
            """Refund one exact payment request."""

            del request_id, amount, runtime
            raise ConnectionError("response was lost")

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_structured_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            graph = compile_tool_graph(tool)
            message = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-failure", request_id="failure-1", amount=5)],
            )
            with pytest.raises(ConnectionError, match="response was lost"):
                await graph.ainvoke({"messages": [message]})
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "ConnectionError"

    asyncio.run(run())


def test_langgraph_runtime_cancellation_is_unknown_and_cannot_commit_later(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        effects: list[str] = []

        async def refund(
            request_id: str,
            amount: int,
            runtime: ToolRuntime,
        ) -> dict[str, object]:
            """Refund one exact payment request."""

            del amount, runtime
            await asyncio.sleep(0.1)
            effects.append(request_id)
            return {"request_id": request_id, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_structured_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            graph = compile_tool_graph(tool)
            message = AIMessage(
                content="",
                tool_calls=[tool_call("model-call-timeout", request_id="timeout-1", amount=5)],
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(graph.ainvoke({"messages": [message]}), timeout=0.01)
            await asyncio.sleep(0.12)
            assert effects == []
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "CancelledError"

    asyncio.run(run())


def test_langgraph_runtime_tool_supports_sync_function_and_selector(tmp_path: Path) -> None:
    calls = 0

    def refund(request_id: str, amount: int = 5) -> dict[str, int]:
        """Refund one exact payment request."""

        nonlocal calls
        calls += 1
        return {"amount": amount}

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        tool = runtime_structured_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key=lambda arguments: str(arguments["request_id"]),
            name="payments_refund",
        )
        graph = compile_tool_graph(tool)
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "payments_refund",
                    "args": {"request_id": "small-1"},
                    "id": "model-call-1",
                    "type": "tool_call",
                }
            ],
        )
        assert graph.invoke({"messages": [message]})["messages"][-1].content == '{"amount": 5}'
        assert graph.invoke({"messages": [message]})["messages"][-1].content == '{"amount": 5}'
        assert calls == 1


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"args_schema": object()}, "require inferred schemas"),
        ({"infer_schema": False}, "require inferred schemas"),
        ({"handle_tool_error": True}, "must be False"),
        ({"response_format": "content_and_artifact"}, "must be 'content'"),
        ({"func": lambda: None}, "owned by"),
    ],
)
def test_langgraph_runtime_tool_rejects_conflicting_controls(
    tmp_path: Path,
    options: dict[str, object],
    message: str,
) -> None:
    def refund(request_id: str) -> str:
        return request_id

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(ValueError, match=message),
    ):
        runtime_structured_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            **options,
        )


@pytest.mark.parametrize("kind", ["callable", "generator", "async-generator"])
def test_langgraph_runtime_tool_rejects_deferred_or_opaque_callables(
    tmp_path: Path,
    kind: str,
) -> None:
    class CallableRefund:
        def __call__(self, request_id: str) -> str:
            return request_id

    def generator_refund(request_id: str) -> object:
        yield request_id

    async def async_generator_refund(request_id: str) -> object:
        yield request_id

    function = {
        "callable": CallableRefund(),
        "generator": generator_refund,
        "async-generator": async_generator_refund,
    }[kind]
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(TypeError, match=r"plain function|generator functions"),
    ):
        runtime_structured_tool(  # type: ignore[arg-type]
            function,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )
