"""Credential-free conformance adapter for PydanticAI."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

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


class PydanticAIAdapter(AgentAdapter):
    """Exercise PydanticAI deferred-tool approvals with a local function model."""

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
            sdk_version = version("pydantic-ai-slim")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"pydantic-ai/{sdk_version}"

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        normalized = validate_actions(actions)
        tool_names = {action.tool_name for action in normalized}
        if len(tool_names) != 1:
            raise AdapterContractError("PydanticAIAdapter requires one shared tool_name per run")
        _load_sdk()
        return _PydanticAIRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _PydanticAIRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: PydanticAIAdapter,
        run_id: str,
        actions: tuple[ActionRequest, ...],
        effect: EffectProbe,
        timeout_seconds: float | None,
    ) -> None:
        self._adapter = adapter
        self._run_id = run_id
        self._actions = actions
        self._effect = effect
        self._timeout_seconds = timeout_seconds
        self._pending_ready = asyncio.Event()
        self._pending: tuple[ActionRequest, ...] = ()
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:pydantic-ai:{run_id}")

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_agent()
            else:
                await asyncio.wait_for(self._run_agent(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.TIMED_OUT, "PydanticAI run exceeded its timeout")
        except asyncio.CancelledError:
            return RunOutcome(RunStatus.CANCELLED, "PydanticAI run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return RunOutcome(RunStatus.COMPLETED)

    async def _run_agent(self) -> None:
        sdk = _load_sdk()
        action_by_id = {action.action_id: action for action in self._actions}

        async def sentinel_write(
            context: Any,
            action_id: str,
            recipient: str,
            amount: int,
        ) -> str:
            original = action_by_id.get(action_id)
            if original is None:
                raise AdapterContractError(f"PydanticAI invoked unknown action id {action_id!r}")
            if original.requires_approval and not context.tool_call_approved:
                raise sdk["ApprovalRequired"]
            effective = original.with_arguments({"recipient": recipient, "amount": amount})
            return await self._effect(effective)

        # Keep PydanticAI optional at module import time while still giving its schema generator
        # the concrete context annotation required for tools registered through `Agent.tool()`.
        sentinel_write.__annotations__["context"] = sdk["RunContext"][None]

        model = _scripted_model(self._actions, sdk)
        agent = sdk["Agent"](
            model,
            name="agentbarrier_conformance_probe",
            output_type=[str, sdk["DeferredToolRequests"]],
        )
        agent.tool(
            name=self._actions[0].tool_name,
            description="Commit a harmless AgentBarrier sentinel effect.",
        )(sentinel_write)

        result = await agent.run(
            "Execute the deterministic sentinel actions.",
            conversation_id=self._run_id,
            run_id=f"{self._run_id}:initial",
        )
        if not isinstance(result.output, sdk["DeferredToolRequests"]):
            return

        requests = result.output
        pending: list[ActionRequest] = []
        loop = asyncio.get_running_loop()
        for call in requests.approvals:
            action_id = str(call.tool_call_id)
            original = action_by_id.get(action_id)
            if original is None:
                raise AdapterContractError(
                    f"PydanticAI returned unknown approval call id {action_id!r}"
                )
            if call.tool_name != original.tool_name:
                raise AdapterContractError(
                    f"PydanticAI changed the tool name for action {action_id!r}"
                )
            # PydanticAI returns its stored argument dictionary directly. Copy it before removing
            # the transport-only action ID so the message history remains valid for resumption.
            arguments = dict(call.args_as_dict(raise_if_invalid=True))
            returned_action_id = arguments.pop("action_id", None)
            if returned_action_id != action_id:
                raise AdapterContractError(
                    f"PydanticAI changed the logical action id for {action_id!r}"
                )
            pending.append(original.with_arguments(_json_mapping(arguments)))
            self._decisions[action_id] = loop.create_future()

        if not pending:
            raise AdapterContractError("PydanticAI returned deferred output without approvals")
        self._pending = tuple(pending)
        self._pending_ready.set()

        deferred_results = sdk["DeferredToolResults"]()
        for action in self._pending:
            decision, replacement, reason = await self._decisions[action.action_id]
            if decision is Decision.APPROVE:
                if replacement is None:
                    deferred_results.approvals[action.action_id] = sdk["ToolApproved"]()
                else:
                    override_args = {
                        "action_id": action.action_id,
                        **dict(replacement.arguments),
                    }
                    deferred_results.approvals[action.action_id] = sdk["ToolApproved"](
                        override_args=override_args
                    )
            else:
                deferred_results.approvals[action.action_id] = sdk["ToolDenied"](
                    reason or "Rejected by AgentBarrier"
                )

        await agent.run(
            message_history=result.all_messages(),
            deferred_tool_results=deferred_results,
            conversation_id=self._run_id,
            run_id=f"{self._run_id}:resume",
        )

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        await wait_for_pending_or_terminal(
            pending_event=self._pending_ready,
            run_task=self._task,
            timeout_seconds=timeout_seconds,
            adapter_name=self._adapter.name,
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
            return RunOutcome(RunStatus.FAILED, "PydanticAI run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("PydanticAI replay is not declared by this adapter")

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


def _load_sdk() -> dict[str, Any]:
    try:
        from pydantic_ai import (
            Agent,
            ApprovalRequired,
            DeferredToolRequests,
            DeferredToolResults,
            RunContext,
            ToolApproved,
            ToolDenied,
        )
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        from pydantic_ai.models.function import FunctionModel
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "PydanticAIAdapter requires `pip install agentbarrier[pydantic-ai]`"
        ) from exc
    return {
        "Agent": Agent,
        "ApprovalRequired": ApprovalRequired,
        "DeferredToolRequests": DeferredToolRequests,
        "DeferredToolResults": DeferredToolResults,
        "FunctionModel": FunctionModel,
        "ModelResponse": ModelResponse,
        "RunContext": RunContext,
        "TextPart": TextPart,
        "ToolApproved": ToolApproved,
        "ToolCallPart": ToolCallPart,
        "ToolDenied": ToolDenied,
    }


def _scripted_model(actions: tuple[ActionRequest, ...], sdk: dict[str, Any]) -> Any:
    def respond(messages: list[Any], _info: Any) -> Any:
        if any(isinstance(message, sdk["ModelResponse"]) for message in messages):
            return sdk["ModelResponse"](
                parts=[sdk["TextPart"]("AgentBarrier sentinel lifecycle completed.")]
            )
        return sdk["ModelResponse"](
            parts=[
                sdk["ToolCallPart"](
                    action.tool_name,
                    {"action_id": action.action_id, **dict(action.arguments)},
                    tool_call_id=action.action_id,
                )
                for action in actions
            ]
        )

    return sdk["FunctionModel"](respond, model_name="agentbarrier-function-model")
