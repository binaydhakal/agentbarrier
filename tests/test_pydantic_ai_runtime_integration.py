from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentbarrier.errors import (
    ActionBindingError,
    ApprovalRequired,
    FrameworkControlSignalError,
)
from agentbarrier.integrations.pydantic_ai import runtime_tool
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

pydantic_ai = pytest.importorskip(
    "pydantic_ai", reason="PydanticAI optional dependency is not installed"
)
pydantic_messages = pytest.importorskip("pydantic_ai.messages")
pydantic_function_model = pytest.importorskip("pydantic_ai.models.function")
Agent = pydantic_ai.Agent
ModelRetry = pydantic_ai.ModelRetry
RunContext = pydantic_ai.RunContext
ModelResponse = pydantic_messages.ModelResponse
TextPart = pydantic_messages.TextPart
ToolCallPart = pydantic_messages.ToolCallPart
ToolReturnPart = pydantic_messages.ToolReturnPart
FunctionModel = pydantic_function_model.FunctionModel


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        version="pydantic-ai-runtime-v1",
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


def scripted_agent(tool: object) -> tuple[Any, dict[str, object]]:
    call: dict[str, object] = {
        "id": "model-call-1",
        "request_id": "refund-42",
        "amount": 100,
    }

    def respond(messages: list[Any], _info: Any) -> Any:
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
                    {"request_id": call["request_id"], "amount": call["amount"]},
                    tool_call_id=str(call["id"]),
                )
            ]
        )

    model = FunctionModel(respond, model_name="agentbarrier-pydantic-runtime")
    return Agent(model, tools=[tool]), call


def test_pydantic_ai_runtime_tool_holds_context_aware_effect_then_replays(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        calls: list[tuple[str, int, str, str]] = []

        async def refund(
            context: RunContext[dict[str, str]],
            request_id: str,
            amount: int,
        ) -> dict[str, object]:
            """Refund one exact payment request."""

            calls.append((request_id, amount, context.deps["tenant"], context.tool_call_id))
            return {"request_id": request_id, "amount": amount, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_tool(
                refund,
                barrier=RuntimeBarrier(
                    policy=make_policy(),
                    store=store,
                    namespace="pydantic-ai",
                ),
                idempotency_key="request_id",
                name="payments_refund",
                description="Refund one exact payment request.",
            )
            properties = tool.tool_def.parameters_json_schema["properties"]
            assert set(properties) == {"request_id", "amount"}
            assert tool.requires_approval is False
            assert tool.timeout is None
            assert tool.max_retries == 0
            agent, call = scripted_agent(tool)

            with pytest.raises(ApprovalRequired) as pending:
                await agent.run("refund", deps={"tenant": "acme"})
            assert calls == []
            assert pending.value.action.arguments == {"request_id": "refund-42", "amount": 100}
            assert "context" not in pending.value.action.arguments
            store.decide(
                pending.value.action.action_id,
                Decision.APPROVE,
                decided_by="finance-reviewer",
            )

            call["id"] = "model-call-2"
            assert (await agent.run("refund", deps={"tenant": "acme"})).output == "done"
            call["id"] = "model-call-3"
            assert (await agent.run("refund", deps={"tenant": "acme"})).output == "done"
            assert calls == [("refund-42", 100, "acme", "model-call-2")]
            assert [receipt.event for receipt in store.receipts()] == [
                RuntimeEvent.APPROVAL_REQUESTED,
                RuntimeEvent.APPROVED,
                RuntimeEvent.EXECUTION_STARTED,
                RuntimeEvent.EXECUTION_SUCCEEDED,
                RuntimeEvent.RESULT_REPLAYED,
            ]

            call.update({"id": "model-call-4", "amount": 101})
            with pytest.raises(ActionBindingError):
                await agent.run("refund", deps={"tenant": "acme"})

    asyncio.run(run())


def test_pydantic_ai_runtime_tool_propagates_post_claim_failure(tmp_path: Path) -> None:
    async def run() -> None:
        async def refund(request_id: str, amount: int) -> dict[str, object]:
            """Refund one exact payment request."""

            del request_id, amount
            raise ConnectionError("response was lost")

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            agent, call = scripted_agent(tool)
            call.update({"request_id": "failure-1", "amount": 5})
            with pytest.raises(ConnectionError, match="response was lost"):
                await agent.run("refund")
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "ConnectionError"

    asyncio.run(run())


def test_pydantic_ai_runtime_cancellation_is_unknown_and_cannot_commit_later(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        effects: list[str] = []

        async def refund(request_id: str, amount: int) -> dict[str, object]:
            """Refund one exact payment request."""

            del amount
            await asyncio.sleep(0.1)
            effects.append(request_id)
            return {"request_id": request_id, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            agent, call = scripted_agent(tool)
            call.update({"request_id": "timeout-1", "amount": 5})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(agent.run("refund"), timeout=0.01)
            await asyncio.sleep(0.12)
            assert effects == []
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "CancelledError"

    asyncio.run(run())


def test_pydantic_ai_runtime_suppresses_framework_retry_after_claim(tmp_path: Path) -> None:
    async def run() -> None:
        async def refund(request_id: str, amount: int) -> dict[str, object]:
            """Refund one exact payment request."""

            del request_id, amount
            raise ModelRetry("please try the effect again")

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            agent, call = scripted_agent(tool)
            call.update({"request_id": "retry-1", "amount": 5})
            with pytest.raises(FrameworkControlSignalError, match="ModelRetry"):
                await agent.run("refund")
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "FrameworkControlSignalError"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"requires_approval": True}, "cannot be combined"),
        ({"timeout": 1}, "must be None"),
        ({"max_retries": 1}, "must be 0"),
        ({"prepare": lambda *_args: None}, "not supported"),
        ({"function_schema": object()}, "not supported"),
        ({"takes_ctx": False}, "must be inferred"),
        ({"schema_generator": object}, "not supported"),
    ],
)
def test_pydantic_ai_runtime_tool_rejects_conflicting_controls(
    tmp_path: Path,
    options: dict[str, object],
    message: str,
) -> None:
    async def refund(request_id: str) -> str:
        return request_id

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(ValueError, match=message),
    ):
        runtime_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            **options,
        )


@pytest.mark.parametrize("kind", ["callable", "sync", "generator", "async-generator"])
def test_pydantic_ai_runtime_tool_rejects_non_cancellable_or_opaque_callables(
    tmp_path: Path,
    kind: str,
) -> None:
    class CallableRefund:
        async def __call__(self, request_id: str) -> str:
            return request_id

    def sync_refund(request_id: str) -> str:
        return request_id

    def generator_refund(request_id: str) -> object:
        yield request_id

    async def async_generator_refund(request_id: str) -> object:
        yield request_id

    function = {
        "callable": CallableRefund(),
        "sync": sync_refund,
        "generator": generator_refund,
        "async-generator": async_generator_refund,
    }[kind]
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(TypeError, match=r"plain function|must be async|generator functions"),
    ):
        runtime_tool(  # type: ignore[arg-type]
            function,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )


@pytest.mark.parametrize(
    ("selector", "error", "message"),
    [
        ("missing", ValueError, "was not bound"),
        (lambda _arguments: 7, TypeError, "must return a string"),
        (lambda _arguments: " ", ValueError, "must not be empty"),
    ],
)
def test_pydantic_ai_runtime_tool_rejects_invalid_business_identity(
    tmp_path: Path,
    selector: object,
    error: type[Exception],
    message: str,
) -> None:
    async def run() -> None:
        async def refund(request_id: str, amount: int) -> dict[str, object]:
            """Refund one exact payment request."""

            return {"request_id": request_id, "amount": amount}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key=selector,  # type: ignore[arg-type]
                name="payments_refund",
            )
            agent, call = scripted_agent(tool)
            call.update({"request_id": "identity-1", "amount": 5})
            with pytest.raises(error, match=message):
                await agent.run("refund")
            assert store.list_actions() == ()

    asyncio.run(run())


def test_pydantic_ai_runtime_tool_rejects_variadic_parameters(tmp_path: Path) -> None:
    async def refund(*request_ids: str) -> list[str]:
        return list(request_ids)

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(TypeError, match="unsupported parameters"),
    ):
        runtime_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )


def test_pydantic_ai_runtime_tool_rejects_flattened_single_object_schema(tmp_path: Path) -> None:
    from pydantic import BaseModel

    class Refund(BaseModel):
        request_id: str
        amount: int

    async def refund(payload: Refund) -> dict[str, object]:
        """Refund one exact payment request."""

        return payload.model_dump()

    refund.__annotations__["payload"] = Refund
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(ValueError, match="one schema property per original tool argument"),
    ):
        runtime_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            name="payments_refund",
        )
