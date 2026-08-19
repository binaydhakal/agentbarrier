"""Credential-free conformance adapter for the OpenAI Agents Python SDK."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
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
    RunOutcome,
    RunStatus,
)
from agentbarrier.probe import EffectProbe

_DecisionValue = tuple[Decision, ActionRequest | None, str | None]


class OpenAIAgentsAdapter(AgentAdapter):
    """Run sentinel calls through the OpenAI Agents SDK approval lifecycle.

    The adapter uses a deterministic local `Model` implementation. It never contacts a model API,
    exports traces, or reads an API key.
    """

    capabilities = frozenset(
        {
            Capability.APPROVAL,
            Capability.REJECTION,
            Capability.CANCELLATION,
            Capability.TIMEOUT,
            Capability.PARALLEL_BARRIER,
        }
    )

    def __init__(self) -> None:
        try:
            sdk_version = version("openai-agents")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"openai-agents/{sdk_version}"

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
            raise AdapterContractError("OpenAIAgentsAdapter requires one shared tool_name per run")
        _load_sdk()
        return _OpenAIRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _OpenAIRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: OpenAIAgentsAdapter,
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
        self._approval_items: dict[str, Any] = {}
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:openai:{run_id}")

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_sdk()
            else:
                await asyncio.wait_for(self._run_sdk(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.TIMED_OUT, "OpenAI Agents run exceeded its timeout")
        except asyncio.CancelledError:
            return RunOutcome(RunStatus.CANCELLED, "OpenAI Agents run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return RunOutcome(RunStatus.COMPLETED)

    async def _run_sdk(self) -> None:
        sdk = _load_sdk()
        action_by_id = {action.action_id: action for action in self._actions}

        async def sentinel_write(action_id: str, recipient: str, amount: int) -> str:
            original = action_by_id[action_id]
            effective = original.with_arguments({"recipient": recipient, "amount": amount})
            return await self._effect(effective)

        async def needs_approval(
            _context: Any,
            arguments: dict[str, Any],
            _call_id: str,
        ) -> bool:
            action_id = str(arguments["action_id"])
            return action_by_id[action_id].requires_approval

        tool = sdk["function_tool"](
            sentinel_write,
            name_override=self._actions[0].tool_name,
            description_override="Commit a harmless AgentBarrier sentinel effect.",
            needs_approval=needs_approval,
        )
        model = _scripted_model(self._actions, sdk)
        agent = sdk["Agent"](
            name="AgentBarrier conformance probe",
            instructions="Execute every supplied sentinel action exactly once.",
            tools=[tool],
            model=model,
            tool_use_behavior="run_llm_again",
        )
        run_config = sdk["RunConfig"](
            tracing_disabled=True,
            workflow_name="AgentBarrier conformance probe",
        )
        result = await sdk["Runner"].run(
            agent,
            "Execute the deterministic sentinel actions.",
            run_config=run_config,
        )
        if not result.interruptions:
            return

        state = result.to_state()
        pending: list[ActionRequest] = []
        loop = asyncio.get_running_loop()
        for item in result.interruptions:
            call_id = str(item.raw_item.call_id)
            action = action_by_id.get(call_id)
            if action is None:
                raise AdapterContractError(f"SDK returned unknown approval call id {call_id!r}")
            pending.append(action)
            self._approval_items[action.action_id] = item
            self._decisions[action.action_id] = loop.create_future()
        self._pending = tuple(pending)
        self._pending_ready.set()

        for action in self._pending:
            decision, replacement, reason = await self._decisions[action.action_id]
            if replacement is not None:
                raise UnsupportedCapability(
                    "the OpenAI Agents manual approval API does not expose argument editing"
                )
            item = self._approval_items[action.action_id]
            if decision is Decision.APPROVE:
                state.approve(item)
            else:
                state.reject(item, rejection_message=reason)
        await sdk["Runner"].run(agent, state, run_config=run_config)

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
            raise UnsupportedCapability(
                "the OpenAI Agents manual approval API does not expose argument editing"
            )
        self._decision_future(action_id).set_result((Decision.APPROVE, None, None))

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
            return RunOutcome(RunStatus.FAILED, "OpenAI Agents run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("OpenAI Agents replay is not declared by this adapter")

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()


def _load_sdk() -> dict[str, Any]:
    try:
        from agents import Agent, Model, ModelResponse, RunConfig, Runner, Usage, function_tool
        from openai.types.responses import (
            ResponseFunctionToolCall,
            ResponseOutputMessage,
            ResponseOutputText,
        )
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "OpenAIAgentsAdapter requires `pip install agentbarrier[openai]`"
        ) from exc
    return {
        "Agent": Agent,
        "Model": Model,
        "ModelResponse": ModelResponse,
        "RunConfig": RunConfig,
        "Runner": Runner,
        "Usage": Usage,
        "function_tool": function_tool,
        "ResponseFunctionToolCall": ResponseFunctionToolCall,
        "ResponseOutputMessage": ResponseOutputMessage,
        "ResponseOutputText": ResponseOutputText,
    }


def _scripted_model(actions: tuple[ActionRequest, ...], sdk: dict[str, Any]) -> Any:
    class ScriptedModel(sdk["Model"]):  # type: ignore[misc, valid-type, name-defined]
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, *_: Any, **__: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                output = [
                    sdk["ResponseFunctionToolCall"](
                        arguments=json.dumps(
                            {
                                "action_id": action.action_id,
                                "recipient": action.arguments["recipient"],
                                "amount": action.arguments["amount"],
                            },
                            sort_keys=True,
                        ),
                        call_id=action.action_id,
                        name=action.tool_name,
                        type="function_call",
                        status="completed",
                    )
                    for action in actions
                ]
            else:
                output = [
                    sdk["ResponseOutputMessage"](
                        id=f"message-{self.calls}",
                        content=[
                            sdk["ResponseOutputText"](
                                annotations=[],
                                text="AgentBarrier sentinel lifecycle completed.",
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return sdk["ModelResponse"](
                output=output,
                usage=sdk["Usage"](),
                response_id=f"agentbarrier-response-{self.calls}",
            )

        def stream_response(self, *_: Any, **__: Any) -> AsyncIterator[Any]:
            async def empty() -> AsyncIterator[Any]:
                if False:  # pragma: no cover - establishes an async iterator without events
                    yield None

            return empty()

    return ScriptedModel()
