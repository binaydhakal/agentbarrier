"""Model-free conformance adapter for LangGraph."""

from __future__ import annotations

import asyncio
import operator
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, TypedDict

from agentbarrier.adapter import (
    AgentAdapter,
    RunHandle,
    validate_actions,
    validate_timeout,
    wait_for_pending_or_terminal,
)
from agentbarrier.errors import AdapterContractError, AgentBarrierError, UnsupportedCapability
from agentbarrier.models import (
    ActionRequest,
    AuditReceipt,
    Capability,
    Decision,
    JsonValue,
    RunOutcome,
    RunStatus,
)
from agentbarrier.probe import EffectProbe

_DecisionValue = tuple[Decision, ActionRequest | None, str | None]


class LangGraphAdapter(AgentAdapter):
    """Exercise LangGraph interrupts, cancellation, timeout, and parallel scheduling."""

    capabilities = frozenset(
        {
            Capability.APPROVAL,
            Capability.REJECTION,
            Capability.ARGUMENT_BINDING,
            Capability.CANCELLATION,
            Capability.TIMEOUT,
            Capability.PARALLEL_BARRIER,
        }
    )

    def __init__(self) -> None:
        try:
            sdk_version = version("langgraph")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"langgraph/{sdk_version}"

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        normalized = validate_actions(actions)
        if sys.version_info < (3, 11):
            raise AgentBarrierError(
                "LangGraphAdapter requires Python 3.11+ because LangGraph interrupts need "
                "runnable-context propagation in async graphs"
            )
        _load_sdk()
        return _LangGraphRun(
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _GraphState(TypedDict):
    completed: Annotated[list[str], operator.add]
    decisions: Annotated[dict[str, dict[str, Any]], _merge_dicts]


class _LangGraphRun(RunHandle):
    def __init__(
        self,
        *,
        run_id: str,
        actions: tuple[ActionRequest, ...],
        effect: EffectProbe,
        timeout_seconds: float | None,
    ) -> None:
        self._run_id = run_id
        self._actions = actions
        self._effect = effect
        self._timeout_seconds = timeout_seconds
        self._pending_ready = asyncio.Event()
        self._pending: tuple[ActionRequest, ...] = ()
        self._interrupt_ids: dict[str, str] = {}
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._sdk = _load_sdk()
        self._graph = self._build_graph()
        self._config = {"configurable": {"thread_id": run_id}}
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:langgraph:{run_id}")

    def _build_graph(self) -> Any:
        builder = self._sdk["StateGraph"](_GraphState)
        for index, action in enumerate(self._actions):
            effect_name = f"effect_{index}"
            builder.add_node(effect_name, self._effect_node(action))
            if action.requires_approval:
                approval_name = f"approval_{index}"
                builder.add_node(approval_name, self._approval_node(action))
                builder.add_edge(self._sdk["START"], approval_name)
                builder.add_edge(approval_name, effect_name)
            else:
                builder.add_edge(self._sdk["START"], effect_name)
            builder.add_edge(effect_name, self._sdk["END"])
        return builder.compile(checkpointer=self._sdk["InMemorySaver"]())

    def _approval_node(self, action: ActionRequest) -> Any:
        # LangGraph's interrupt context is not propagated into async nodes on Python 3.10.
        # Keeping the interrupt in a synchronous node supports every declared Python version.
        def request_approval(_: _GraphState) -> _GraphState:
            response = self._sdk["interrupt"](
                {
                    "action_id": action.action_id,
                    "tool_name": action.tool_name,
                    "arguments": dict(action.arguments),
                }
            )
            if not isinstance(response, dict):
                raise AdapterContractError("LangGraph approval response must be an object")
            return {"completed": [], "decisions": {action.action_id: response}}

        return request_approval

    def _effect_node(self, action: ActionRequest) -> Any:
        async def execute(state: _GraphState) -> _GraphState:
            effective = action
            if action.requires_approval:
                response = state["decisions"][action.action_id]
                if response.get("decision") != "approve":
                    return {"completed": [], "decisions": {}}
                raw_arguments = response.get("arguments", dict(action.arguments))
                if not isinstance(raw_arguments, dict):
                    raise AdapterContractError("LangGraph approval arguments must be an object")
                effective = action.with_arguments(_json_mapping(raw_arguments))
            await self._effect(effective)
            return {"completed": [action.action_id], "decisions": {}}

        return execute

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_graph()
            else:
                await asyncio.wait_for(self._run_graph(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.TIMED_OUT, "LangGraph run exceeded its timeout")
        except asyncio.CancelledError:
            return RunOutcome(RunStatus.CANCELLED, "LangGraph run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return RunOutcome(RunStatus.COMPLETED)

    async def _run_graph(self) -> None:
        result = await self._graph.ainvoke({"completed": [], "decisions": {}}, config=self._config)
        interruptions = tuple(result.get("__interrupt__", ()))
        if not interruptions:
            return

        action_by_id = {action.action_id: action for action in self._actions}
        pending: list[ActionRequest] = []
        loop = asyncio.get_running_loop()
        for item in interruptions:
            value = item.value
            if not isinstance(value, dict) or "action_id" not in value:
                raise AdapterContractError("LangGraph returned an unrecognized interrupt payload")
            action_id = str(value["action_id"])
            action = action_by_id.get(action_id)
            if action is None:
                raise AdapterContractError(f"LangGraph returned unknown action id {action_id!r}")
            pending.append(action)
            self._interrupt_ids[action_id] = str(item.id)
            self._decisions[action_id] = loop.create_future()
        self._pending = tuple(pending)
        self._pending_ready.set()

        resume: dict[str, dict[str, Any]] = {}
        for action in self._pending:
            decision, replacement, reason = await self._decisions[action.action_id]
            payload: dict[str, Any] = {"decision": decision.value}
            if replacement is not None:
                payload["arguments"] = dict(replacement.arguments)
            if reason is not None:
                payload["reason"] = reason
            resume[self._interrupt_ids[action.action_id]] = payload
        await self._graph.ainvoke(self._sdk["Command"](resume=resume), config=self._config)

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        await wait_for_pending_or_terminal(
            pending_event=self._pending_ready,
            run_task=self._task,
            timeout_seconds=timeout_seconds,
            adapter_name="LangGraph",
        )
        return self._pending

    def _decision_future(self, action_id: str) -> asyncio.Future[_DecisionValue]:
        future = self._decisions.get(action_id)
        if future is None:
            raise AdapterContractError(f"{action_id!r} is not pending approval")
        if future.done():
            raise AdapterContractError(f"{action_id!r} already has a decision")
        return future

    async def approve(
        self,
        action_id: str,
        replacement: ActionRequest | None = None,
    ) -> None:
        if replacement is not None:
            original = next(
                (action for action in self._pending if action.action_id == action_id), None
            )
            if original is None:
                raise AdapterContractError(f"{action_id!r} is not pending approval")
            if replacement.action_id != original.action_id:
                raise AdapterContractError("replacement must preserve action_id")
            if replacement.tool_name != original.tool_name:
                raise AdapterContractError("replacement must preserve tool_name")
        self._decision_future(action_id).set_result((Decision.APPROVE, replacement, None))

    async def reject(self, action_id: str, reason: str | None = None) -> None:
        self._decision_future(action_id).set_result((Decision.REJECT, None, reason))

    async def cancel(self) -> None:
        if self._task.done():
            return
        self._task.cancel()
        await self._task

    async def wait(self, timeout_seconds: float) -> RunOutcome:
        try:
            return await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.FAILED, "LangGraph run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("LangGraph replay is not declared by this adapter")

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()


def _json_mapping(value: dict[Any, Any]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AdapterContractError("action argument keys must be strings")
        result[key] = item
    return result


def _merge_dicts(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {**left, **right}


def _load_sdk() -> dict[str, Any]:
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "LangGraphAdapter requires `pip install agentbarrier[langgraph]`"
        ) from exc
    return {
        "Command": Command,
        "END": END,
        "InMemorySaver": InMemorySaver,
        "START": START,
        "StateGraph": StateGraph,
        "interrupt": interrupt,
    }
