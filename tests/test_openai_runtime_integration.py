from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentbarrier.errors import ActionBindingError, ApprovalRequired
from agentbarrier.integrations.openai_agents import runtime_function_tool
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

agents = pytest.importorskip("agents", reason="openai-agents optional dependency is not installed")
RunContextWrapper = agents.RunContextWrapper
ToolContext = pytest.importorskip("agents.tool_context").ToolContext


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        version="openai-runtime-v1",
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


def test_openai_runtime_tool_holds_context_aware_effect_then_replays(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[tuple[str, int, str]] = []

        async def refund(
            context: RunContextWrapper[dict[str, str]],
            request_id: str,
            amount: int,
        ) -> dict[str, object]:
            calls.append((request_id, amount, context.context["tenant"]))
            return {"request_id": request_id, "amount": amount, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            barrier = RuntimeBarrier(
                policy=make_policy(),
                store=store,
                namespace="openai-agents",
            )
            tool = runtime_function_tool(
                refund,
                barrier=barrier,
                idempotency_key="request_id",
                name_override="payments_refund",
                description_override="Refund one exact payment request.",
            )
            assert isinstance(tool, agents.FunctionTool)
            assert tool.name == "payments_refund"
            assert tool.description == "Refund one exact payment request."
            assert set(tool.params_json_schema["properties"]) == {"request_id", "amount"}
            assert tool.needs_approval is False
            assert tool.timeout_behavior == "raise_exception"

            def context(call_id: str, amount: int) -> ToolContext[dict[str, str]]:
                arguments = json.dumps({"request_id": "refund-42", "amount": amount})
                return ToolContext(
                    context={"tenant": "acme"},
                    tool_name=tool.name,
                    tool_call_id=call_id,
                    tool_arguments=arguments,
                )

            first_arguments = json.dumps({"request_id": "refund-42", "amount": 100})
            with pytest.raises(ApprovalRequired) as pending:
                await tool.on_invoke_tool(context("sdk-call-1", 100), first_arguments)
            assert calls == []
            assert pending.value.action.arguments == {"request_id": "refund-42", "amount": 100}
            assert "context" not in pending.value.action.arguments
            store.decide(
                pending.value.action.action_id,
                Decision.APPROVE,
                decided_by="finance-reviewer",
            )

            expected = {"request_id": "refund-42", "amount": 100, "refunded": True}
            assert (
                await tool.on_invoke_tool(context("sdk-call-2", 100), first_arguments) == expected
            )
            assert (
                await tool.on_invoke_tool(context("sdk-call-3", 100), first_arguments) == expected
            )
            assert calls == [("refund-42", 100, "acme")]
            assert [receipt.event for receipt in store.receipts()] == [
                RuntimeEvent.APPROVAL_REQUESTED,
                RuntimeEvent.APPROVED,
                RuntimeEvent.EXECUTION_STARTED,
                RuntimeEvent.EXECUTION_SUCCEEDED,
                RuntimeEvent.RESULT_REPLAYED,
            ]

            changed = json.dumps({"request_id": "refund-42", "amount": 101})
            with pytest.raises(ActionBindingError):
                await tool.on_invoke_tool(context("sdk-call-4", 101), changed)

    asyncio.run(run())


def test_openai_runtime_tool_supports_sync_function_and_selector(tmp_path: Path) -> None:
    calls = 0

    def refund(request_id: str, amount: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"amount": amount}

    async def run() -> None:
        nonlocal calls
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_function_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key=lambda arguments: str(arguments["request_id"]),
                name_override="payments_refund",
            )
            raw = json.dumps({"request_id": "small-1", "amount": 5})
            context = ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="sdk-call-1",
                tool_arguments=raw,
            )
            assert await tool.on_invoke_tool(context, raw) == {"amount": 5}
            assert await tool.on_invoke_tool(context, raw) == {"amount": 5}
            assert calls == 1

    asyncio.run(run())


def test_openai_runtime_tool_does_not_swallow_post_claim_failure(tmp_path: Path) -> None:
    async def fail_after_claim(request_id: str, amount: int) -> dict[str, int]:
        raise ConnectionError("response was lost")

    async def run() -> None:
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_function_tool(
                fail_after_claim,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name_override="payments_refund",
            )
            raw = json.dumps({"request_id": "failure-1", "amount": 5})
            context = ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="sdk-call-failure",
                tool_arguments=raw,
            )
            with pytest.raises(ConnectionError, match="response was lost"):
                await tool.on_invoke_tool(context, raw)
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "ConnectionError"

    asyncio.run(run())


def test_openai_runtime_timeout_is_unknown_and_cannot_commit_later(tmp_path: Path) -> None:
    from agents.exceptions import ToolTimeoutError
    from agents.tool import invoke_function_tool

    async def run() -> None:
        effects: list[str] = []

        async def slow_refund(request_id: str, amount: int) -> dict[str, int]:
            await asyncio.sleep(0.1)
            effects.append(request_id)
            return {"amount": amount}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_function_tool(
                slow_refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name_override="payments_refund",
                timeout=0.01,
            )
            raw = json.dumps({"request_id": "timeout-1", "amount": 5})
            context = ToolContext(
                context=None,
                tool_name=tool.name,
                tool_call_id="sdk-call-timeout",
                tool_arguments=raw,
            )
            with pytest.raises(ToolTimeoutError):
                await invoke_function_tool(
                    function_tool=tool,
                    context=context,
                    arguments=raw,
                )
            await asyncio.sleep(0.12)
            assert effects == []
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "CancelledError"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"needs_approval": True}, "cannot be combined"),
        ({"failure_error_function": lambda *_args: "ignored"}, "must be None"),
        ({"timeout_behavior": "error_as_result"}, "must be 'raise_exception'"),
    ],
)
def test_openai_runtime_tool_rejects_conflicting_sdk_controls(
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
        runtime_function_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            **options,
        )


@pytest.mark.parametrize("kind", ["callable", "generator", "async-generator"])
def test_openai_runtime_tool_rejects_deferred_or_opaque_callables(
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
        runtime_function_tool(  # type: ignore[arg-type]
            function,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )
