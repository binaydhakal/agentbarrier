from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentbarrier.errors import (
    ActionBindingError,
    ActionOutcomeUnknown,
    ApprovalRejected,
    ApprovalRequired,
    PolicyDenied,
)
from agentbarrier.models import Decision
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    RuntimeStatus,
    SQLiteRuntimeStore,
)


def make_policy() -> RuntimePolicy:
    return RuntimePolicy(
        version="1",
        rules=(
            PolicyRule(
                "review large refunds",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="payments.refund",
                conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
            ),
            PolicyRule("allow small refunds", PolicyEffect.ALLOW, tool="payments.refund"),
            PolicyRule("block deletes", PolicyEffect.DENY, tool="database.delete"),
        ),
    )


def test_runtime_barrier_allows_and_replays_sync_function(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    def refund(request_id: str, amount: int) -> dict[str, object]:
        calls.append((request_id, amount))
        return {"request_id": request_id, "amount": amount, "refunded": True}

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=make_policy(), store=store, namespace="billing")
        protected = barrier.protect(
            refund,
            tool_name="payments.refund",
            idempotency_key="request_id",
        )

        expected = {"request_id": "r-small", "amount": 10, "refunded": True}
        assert protected("r-small", 10) == expected
        assert protected("r-small", 10) == expected
        assert calls == [("r-small", 10)]
        assert store.list_actions()[0].status is RuntimeStatus.SUCCEEDED


def test_runtime_barrier_pauses_for_cli_style_decision_then_executes(tmp_path: Path) -> None:
    calls = 0

    def refund(request_id: str, amount: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"amount": amount}

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        protected = RuntimeBarrier(policy=make_policy(), store=store).protect(
            refund,
            tool_name="payments.refund",
            idempotency_key="request_id",
        )
        with pytest.raises(ApprovalRequired) as pending:
            protected("r-large", 100)
        assert calls == 0
        action_id = pending.value.action.action_id
        store.decide(action_id, Decision.APPROVE, decided_by="finance-reviewer")

        assert protected("r-large", 100) == {"amount": 100}
        assert protected("r-large", 100) == {"amount": 100}
        assert calls == 1


def test_runtime_barrier_rejection_denial_and_exact_binding(tmp_path: Path) -> None:
    def refund(request_id: str, amount: int) -> dict[str, int]:
        return {"amount": amount}

    def delete(request_id: str, table: str) -> None:
        raise AssertionError("denied function must not execute")

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=make_policy(), store=store)
        protected_refund = barrier.protect(
            refund, tool_name="payments.refund", idempotency_key="request_id"
        )
        with pytest.raises(ApprovalRequired) as pending:
            protected_refund("same-key", 100)
        store.decide(pending.value.action.action_id, Decision.REJECT, decided_by="reviewer")
        with pytest.raises(ApprovalRejected):
            protected_refund("same-key", 100)
        with pytest.raises(ActionBindingError):
            protected_refund("same-key", 101)

        protected_delete = barrier.protect(
            delete, tool_name="database.delete", idempotency_key="request_id"
        )
        with pytest.raises(PolicyDenied):
            protected_delete("delete-1", "customers")


def test_runtime_barrier_supports_async_functions(tmp_path: Path) -> None:
    calls = 0

    async def refund(request_id: str, amount: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"amount": amount}

    async def run() -> None:
        nonlocal calls
        with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
            protected = RuntimeBarrier(policy=make_policy(), store=store).protect(
                refund,
                tool_name="payments.refund",
                idempotency_key=lambda arguments: str(arguments["request_id"]),
            )
            assert await protected("async-small", 5) == {"amount": 5}
            assert await protected("async-small", 5) == {"amount": 5}
            assert calls == 1

    asyncio.run(run())


def test_runtime_barrier_marks_exceptions_and_invalid_results_unknown(tmp_path: Path) -> None:
    def failing(request_id: str) -> None:
        raise ConnectionError("response lost")

    def invalid_result(request_id: str) -> object:
        return object()

    policy = RuntimePolicy(
        "1",
        (PolicyRule("allow failures", PolicyEffect.ALLOW, tool="demo.*"),),
    )
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=policy, store=store)
        protected_failure = barrier.protect(
            failing, tool_name="demo.failure", idempotency_key="request_id"
        )
        with pytest.raises(ConnectionError, match="response lost"):
            protected_failure("failure-1")
        assert store.list_actions()[0].status is RuntimeStatus.UNKNOWN
        with pytest.raises(ActionOutcomeUnknown):
            protected_failure("failure-1")

        protected_invalid = barrier.protect(
            invalid_result, tool_name="demo.invalid", idempotency_key="request_id"
        )
        with pytest.raises(ActionOutcomeUnknown):
            protected_invalid("invalid-1")
        assert store.list_actions()[1].status is RuntimeStatus.UNKNOWN


@pytest.mark.parametrize(
    ("selector", "arguments", "error"),
    [
        ("missing", ("id", 1), ValueError),
        ("amount", ("id", 1), TypeError),
        (lambda _arguments: "", ("id", 1), ValueError),
        (lambda _arguments: 1, ("id", 1), TypeError),
    ],
)
def test_runtime_barrier_validates_idempotency_selector(
    tmp_path: Path, selector: object, arguments: tuple[str, int], error: type[Exception]
) -> None:
    def refund(request_id: str, amount: int) -> dict[str, int]:
        return {"amount": amount}

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        protected = RuntimeBarrier(policy=make_policy(), store=store).protect(
            refund,
            tool_name="payments.refund",
            idempotency_key=selector,  # type: ignore[arg-type]
        )
        with pytest.raises(error):
            protected(*arguments)


def test_runtime_barrier_rejects_non_json_arguments_and_invalid_configuration(
    tmp_path: Path,
) -> None:
    def tool(request_id: str, value: object) -> None:
        return None

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=make_policy(), store=store)
        with pytest.raises(ValueError, match="namespace"):
            RuntimeBarrier(policy=make_policy(), store=store, namespace="")
        with pytest.raises(ValueError, match="tool_name"):
            barrier.protect(tool, tool_name="", idempotency_key="request_id")
        protected = barrier.protect(
            tool,
            tool_name="unknown.tool",
            idempotency_key="request_id",
        )
        with pytest.raises(TypeError, match="unsupported"):
            protected("bad-json", object())


def test_runtime_barrier_rejects_generator_functions_before_submission(tmp_path: Path) -> None:
    def generator_tool(request_id: str):
        yield {"request_id": request_id}

    async def async_generator_tool(request_id: str):
        yield {"request_id": request_id}

    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=make_policy(), store=store)
        with pytest.raises(TypeError, match="generator"):
            barrier.protect(
                generator_tool,
                tool_name="payments.refund",
                idempotency_key="request_id",
            )
        with pytest.raises(TypeError, match="generator"):
            barrier.protect(
                async_generator_tool,
                tool_name="payments.refund",
                idempotency_key="request_id",
            )
        assert store.list_actions() == ()
