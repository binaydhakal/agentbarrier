"""Credential-free conformance adapter for Google Agent Development Kit."""

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


class GoogleADKAdapter(AgentAdapter):
    """Exercise Google ADK tool confirmations with a deterministic local model."""

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
            sdk_version = version("google-adk")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"google-adk/{sdk_version}"

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
            raise AdapterContractError("GoogleADKAdapter requires one shared tool_name per run")
        _load_sdk()
        return _GoogleADKRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _GoogleADKRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: GoogleADKAdapter,
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
        self._confirmation_ids: dict[str, str] = {}
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:google-adk:{run_id}")

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_adk()
            else:
                await asyncio.wait_for(self._run_adk(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.TIMED_OUT, "Google ADK run exceeded its timeout")
        except asyncio.CancelledError:
            return RunOutcome(RunStatus.CANCELLED, "Google ADK run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return RunOutcome(RunStatus.COMPLETED)

    async def _run_adk(self) -> None:
        sdk = _load_sdk()
        action_by_id = {action.action_id: action for action in self._actions}

        async def sentinel_write(
            action_id: str,
            recipient: str,
            amount: int,
            tool_context: Any,
        ) -> str:
            del tool_context
            original = action_by_id.get(action_id)
            if original is None:
                raise AdapterContractError(f"Google ADK invoked unknown action id {action_id!r}")
            effective = original.with_arguments({"recipient": recipient, "amount": amount})
            return await self._effect(effective)

        def needs_confirmation(
            action_id: str,
            recipient: str,
            amount: int,
            tool_context: Any,
        ) -> bool:
            del recipient, amount, tool_context
            original = action_by_id.get(action_id)
            if original is None:
                raise AdapterContractError(f"Google ADK checked unknown action id {action_id!r}")
            return original.requires_approval

        sentinel_write.__name__ = self._actions[0].tool_name
        model = _scripted_model(self._actions, sdk)
        tool = sdk["FunctionTool"](
            func=sentinel_write,
            require_confirmation=needs_confirmation,
        )
        agent = sdk["LlmAgent"](
            name="agentbarrier_probe",
            model=model,
            tools=[tool],
        )
        runner = sdk["InMemoryRunner"](agent=agent, app_name="agentbarrier_probe")
        try:
            await runner.session_service.create_session(
                app_name="agentbarrier_probe",
                user_id="agentbarrier",
                session_id=self._run_id,
            )
            events = await _collect_events(
                runner.run_async(
                    user_id="agentbarrier",
                    session_id=self._run_id,
                    new_message=sdk["Content"](
                        role="user",
                        parts=[sdk["Part"](text="Execute the deterministic sentinel actions.")],
                    ),
                )
            )
            confirmation_events = _confirmation_events(events, sdk)
            if not confirmation_events:
                return

            pending: list[ActionRequest] = []
            loop = asyncio.get_running_loop()
            invocation_ids: set[str] = set()
            for event, confirmation_call in confirmation_events:
                if event.invocation_id:
                    invocation_ids.add(str(event.invocation_id))
                confirmation_id = confirmation_call.id
                if not confirmation_id:
                    raise AdapterContractError("Google ADK returned a confirmation without an id")
                raw_args = confirmation_call.args or {}
                raw_original = raw_args.get("originalFunctionCall")
                if not isinstance(raw_original, dict):
                    raise AdapterContractError(
                        "Google ADK returned an unrecognized confirmation payload"
                    )
                action_id = str(raw_original.get("id", ""))
                original = action_by_id.get(action_id)
                if original is None:
                    raise AdapterContractError(
                        f"Google ADK returned unknown confirmation action id {action_id!r}"
                    )
                if raw_original.get("name") != original.tool_name:
                    raise AdapterContractError(
                        f"Google ADK changed the tool name for action {action_id!r}"
                    )
                arguments = raw_original.get("args")
                if not isinstance(arguments, dict):
                    raise AdapterContractError("Google ADK returned non-object tool arguments")
                arguments = dict(arguments)
                returned_action_id = arguments.pop("action_id", None)
                if returned_action_id != action_id:
                    raise AdapterContractError(
                        f"Google ADK changed the logical action id for {action_id!r}"
                    )
                if action_id in self._confirmation_ids:
                    raise AdapterContractError(
                        f"Google ADK returned duplicate confirmation for {action_id!r}"
                    )
                pending.append(original.with_arguments(_json_mapping(arguments)))
                self._confirmation_ids[action_id] = str(confirmation_id)
                self._decisions[action_id] = loop.create_future()

            if len(invocation_ids) != 1:
                raise AdapterContractError(
                    "Google ADK confirmations did not share one resumable invocation"
                )
            self._pending = tuple(pending)
            self._pending_ready.set()

            response_parts: list[Any] = []
            for action in self._pending:
                decision, replacement, _reason = await self._decisions[action.action_id]
                if replacement is not None:
                    raise UnsupportedCapability(
                        "Google ADK confirmation does not expose argument editing"
                    )
                response_parts.append(
                    sdk["Part"](
                        function_response=sdk["FunctionResponse"](
                            id=self._confirmation_ids[action.action_id],
                            name=sdk["REQUEST_CONFIRMATION_FUNCTION_CALL_NAME"],
                            response={"confirmed": decision is Decision.APPROVE},
                        )
                    )
                )

            await _collect_events(
                runner.run_async(
                    user_id="agentbarrier",
                    session_id=self._run_id,
                    new_message=sdk["Content"](role="user", parts=response_parts),
                )
            )
        finally:
            await runner.close()

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
            raise UnsupportedCapability("Google ADK confirmation does not expose argument editing")
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
            return RunOutcome(RunStatus.FAILED, "Google ADK run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("Google ADK replay is not declared by this adapter")

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()


async def _collect_events(stream: Any) -> list[Any]:
    return [event async for event in stream]


def _confirmation_events(events: list[Any], sdk: dict[str, Any]) -> list[tuple[Any, Any]]:
    result: list[tuple[Any, Any]] = []
    for event in events:
        content = event.content
        if content is None or not content.parts:
            continue
        for part in content.parts:
            call = part.function_call
            if call is not None and call.name == sdk["REQUEST_CONFIRMATION_FUNCTION_CALL_NAME"]:
                result.append((event, call))
    return result


def _json_mapping(value: dict[Any, Any]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AdapterContractError("action argument keys must be strings")
        result[key] = item
    return result


def _load_sdk() -> dict[str, Any]:
    try:
        from google.adk.agents.llm_agent import LlmAgent
        from google.adk.flows.llm_flows.functions import (
            REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
        )
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import InMemoryRunner
        from google.adk.tools.function_tool import FunctionTool
        from google.genai.types import (
            Content,
            FunctionCall,
            FunctionResponse,
            GenerateContentResponseUsageMetadata,
            Part,
        )
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "GoogleADKAdapter requires `pip install agentbarrier[google-adk]`"
        ) from exc
    return {
        "BaseLlm": BaseLlm,
        "Content": Content,
        "FunctionCall": FunctionCall,
        "FunctionResponse": FunctionResponse,
        "FunctionTool": FunctionTool,
        "GenerateContentResponseUsageMetadata": GenerateContentResponseUsageMetadata,
        "InMemoryRunner": InMemoryRunner,
        "LlmAgent": LlmAgent,
        "LlmResponse": LlmResponse,
        "Part": Part,
        "REQUEST_CONFIRMATION_FUNCTION_CALL_NAME": REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
    }


def _scripted_model(actions: tuple[ActionRequest, ...], sdk: dict[str, Any]) -> Any:
    class ScriptedLlm(sdk["BaseLlm"]):  # type: ignore[misc, valid-type, name-defined]
        call_count: int = 0

        async def generate_content_async(self, _request: Any, stream: bool = False) -> Any:
            del stream
            self.call_count += 1
            if self.call_count == 1:
                parts = [
                    sdk["Part"](
                        function_call=sdk["FunctionCall"](
                            id=action.action_id,
                            name=action.tool_name,
                            args={"action_id": action.action_id, **dict(action.arguments)},
                        )
                    )
                    for action in actions
                ]
            else:
                parts = [sdk["Part"](text="AgentBarrier sentinel lifecycle completed.")]
            yield sdk["LlmResponse"](
                content=sdk["Content"](role="model", parts=parts),
                usage_metadata=sdk["GenerateContentResponseUsageMetadata"](
                    prompt_token_count=1,
                    candidates_token_count=1,
                    total_token_count=2,
                ),
            )

    return ScriptedLlm(model="agentbarrier-local")
