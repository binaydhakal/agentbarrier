from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentbarrier.errors import ActionBindingError, ApprovalRequired
from agentbarrier.integrations.google_adk import runtime_function_tool
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

pytest.importorskip("google.adk", reason="Google ADK optional dependency is not installed")
from google.adk.sessions.session import Session
from google.adk.tools import ToolContext


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        version="google-adk-runtime-v1",
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


def make_tool_context(function_call_id: str) -> ToolContext:
    session = Session(id="session", appName="agentbarrier", userId="operator")
    invocation_context = SimpleNamespace(session=session)
    return ToolContext(cast(Any, invocation_context), function_call_id=function_call_id)


def declaration_properties(tool: Any) -> dict[str, object]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\].*")
        declaration = tool._get_declaration()
    schema = declaration.parameters_json_schema
    properties = schema["properties"] if schema is not None else declaration.parameters.properties
    return cast(dict[str, object], properties)


def test_google_adk_runtime_tool_holds_context_aware_effect_then_replays(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        calls: list[tuple[str, int, str]] = []

        async def refund(
            request_id: str,
            context: ToolContext,
            amount: int = 100,
        ) -> dict[str, object]:
            """Refund one exact payment request."""

            calls.append((request_id, amount, cast(str, context.function_call_id)))
            return {"request_id": request_id, "amount": amount, "refunded": True}

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_function_tool(
                refund,
                barrier=RuntimeBarrier(
                    policy=make_policy(),
                    store=store,
                    namespace="google-adk",
                ),
                idempotency_key="request_id",
                name="payments_refund",
                description="Refund one exact payment request.",
            )
            assert set(declaration_properties(tool)) == {"request_id", "amount"}
            assert tool.name == "payments_refund"
            assert tool.description == "Refund one exact payment request."
            assert tool._require_confirmation is False

            with pytest.raises(ApprovalRequired) as pending:
                await tool.run_async(
                    args={"request_id": "refund-42"},
                    tool_context=make_tool_context("model-call-1"),
                )
            assert calls == []
            assert pending.value.action.arguments == {"request_id": "refund-42", "amount": 100}
            assert "context" not in pending.value.action.arguments
            store.decide(
                pending.value.action.action_id,
                Decision.APPROVE,
                decided_by="finance-reviewer",
            )

            executed = await tool.run_async(
                args={"request_id": "refund-42"},
                tool_context=make_tool_context("model-call-2"),
            )
            replayed = await tool.run_async(
                args={"request_id": "refund-42"},
                tool_context=make_tool_context("model-call-3"),
            )
            assert (
                executed
                == replayed
                == {
                    "request_id": "refund-42",
                    "amount": 100,
                    "refunded": True,
                }
            )
            assert calls == [("refund-42", 100, "model-call-2")]
            assert [receipt.event for receipt in store.receipts()] == [
                RuntimeEvent.APPROVAL_REQUESTED,
                RuntimeEvent.APPROVED,
                RuntimeEvent.EXECUTION_STARTED,
                RuntimeEvent.EXECUTION_SUCCEEDED,
                RuntimeEvent.RESULT_REPLAYED,
            ]

            with pytest.raises(ActionBindingError):
                await tool.run_async(
                    args={"request_id": "refund-42", "amount": 101},
                    tool_context=make_tool_context("model-call-4"),
                )

    asyncio.run(run())


def test_google_adk_runtime_tool_propagates_post_claim_failure(tmp_path: Path) -> None:
    async def run() -> None:
        async def refund(request_id: str, amount: int) -> dict[str, object]:
            """Refund one exact payment request."""

            del request_id, amount
            raise ConnectionError("response was lost")

        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            tool = runtime_function_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            with pytest.raises(ConnectionError, match="response was lost"):
                await tool.run_async(
                    args={"request_id": "failure-1", "amount": 5},
                    tool_context=make_tool_context("model-call-1"),
                )
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "ConnectionError"

    asyncio.run(run())


def test_google_adk_runtime_cancellation_is_unknown_and_cannot_commit_later(
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
            tool = runtime_function_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key="request_id",
                name="payments_refund",
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    tool.run_async(
                        args={"request_id": "timeout-1", "amount": 5},
                        tool_context=make_tool_context("model-call-1"),
                    ),
                    timeout=0.01,
                )
            await asyncio.sleep(0.12)
            assert effects == []
            action = store.list_actions()[0]
            assert action.status is RuntimeStatus.UNKNOWN
            assert action.error == "CancelledError"

    asyncio.run(run())


@pytest.mark.parametrize("confirmation", [True, lambda *_args: True])
def test_google_adk_runtime_tool_rejects_native_confirmation(
    tmp_path: Path,
    confirmation: object,
) -> None:
    async def refund(request_id: str) -> str:
        return request_id

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(ValueError, match="cannot be combined"),
    ):
        runtime_function_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            require_confirmation=confirmation,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"name": ""}, ValueError, "non-empty name"),
        ({"description": 7}, TypeError, "description must be a string"),
    ],
)
def test_google_adk_runtime_tool_rejects_invalid_metadata(
    tmp_path: Path,
    options: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    async def refund(request_id: str) -> str:
        return request_id

    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(error, match=message),
    ):
        runtime_function_tool(
            refund,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
            **options,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("kind", ["callable", "sync", "generator", "async-generator"])
def test_google_adk_runtime_tool_rejects_non_cancellable_or_opaque_callables(
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
        runtime_function_tool(  # type: ignore[arg-type]
            function,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )


@pytest.mark.parametrize("kind", ["variadic", "input-stream"])
def test_google_adk_runtime_tool_rejects_unsafe_signatures(tmp_path: Path, kind: str) -> None:
    async def variadic_refund(*request_ids: str) -> list[str]:
        return list(request_ids)

    async def streaming_refund(request_id: str, input_stream: object) -> str:
        del input_stream
        return request_id

    function = variadic_refund if kind == "variadic" else streaming_refund
    with (
        SQLiteRuntimeStore(tmp_path / "runtime.db") as store,
        pytest.raises(TypeError, match=r"unsupported parameters|streaming input"),
    ):
        runtime_function_tool(
            function,
            barrier=RuntimeBarrier(policy=make_policy(), store=store),
            idempotency_key="request_id",
        )


@pytest.mark.parametrize(
    ("selector", "error", "message"),
    [
        ("missing", ValueError, "was not bound"),
        ("amount", TypeError, "must be a string"),
        (lambda _arguments: 7, TypeError, "must return a string"),
        (lambda _arguments: " ", ValueError, "must not be empty"),
    ],
)
def test_google_adk_runtime_tool_rejects_invalid_business_identity(
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
            tool = runtime_function_tool(
                refund,
                barrier=RuntimeBarrier(policy=make_policy(), store=store),
                idempotency_key=selector,  # type: ignore[arg-type]
                name="payments_refund",
            )
            with pytest.raises(error, match=message):
                await tool.run_async(
                    args={"request_id": "identity-1", "amount": 5},
                    tool_context=make_tool_context("model-call-1"),
                )
            assert store.list_actions() == ()

    asyncio.run(run())
