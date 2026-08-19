"""Credential-free conformance adapter for Microsoft AutoGen Core."""

from __future__ import annotations

import asyncio
import json
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


class AutoGenAdapter(AgentAdapter):
    """Exercise AutoGen Core intervention handlers against a local tool runtime."""

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
            sdk_version = version("autogen-core")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"autogen-core/{sdk_version}"

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
            raise AdapterContractError("AutoGenAdapter requires one shared tool_name per run")
        _load_sdk()
        return _AutoGenRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _AutoGenRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: AutoGenAdapter,
        run_id: str,
        actions: tuple[ActionRequest, ...],
        effect: EffectProbe,
        timeout_seconds: float | None,
    ) -> None:
        self._adapter = adapter
        self._run_id = run_id
        self._actions = actions
        self._action_by_id = {action.action_id: action for action in actions}
        self._effect = effect
        self._timeout_seconds = timeout_seconds
        self._pending_ready = asyncio.Event()
        self._pending_by_id: dict[str, ActionRequest] = {}
        self._pending: tuple[ActionRequest, ...] = ()
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._rejected: set[str] = set()
        self._expected_pending = sum(action.requires_approval for action in actions)
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:autogen:{run_id}")

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_autogen()
            else:
                await asyncio.wait_for(self._run_autogen(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.TIMED_OUT, "AutoGen run exceeded its timeout")
        except asyncio.CancelledError:
            return RunOutcome(RunStatus.CANCELLED, "AutoGen run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return RunOutcome(RunStatus.COMPLETED)

    async def _run_autogen(self) -> None:
        sdk = _load_sdk()
        run = self
        intervention_handler_base = sdk["DefaultInterventionHandler"]

        class ApprovalInterventionHandler(
            intervention_handler_base  # type: ignore[misc, valid-type]
        ):
            async def on_send(
                self,
                message: Any,
                *,
                message_context: Any,
                recipient: Any,
            ) -> Any:
                del message_context, recipient
                if not isinstance(message, sdk["FunctionCall"]):
                    return message
                return await run._intercept(message, sdk)

        async def sentinel_write(
            action_id: str,
            recipient: str,
            amount: int,
            cancellation_token: Any,
        ) -> str:
            original = self._action_by_id.get(action_id)
            if original is None:
                raise AdapterContractError(f"AutoGen invoked unknown action id {action_id!r}")
            effective = original.with_arguments({"recipient": recipient, "amount": amount})
            effect_task = asyncio.create_task(self._effect(effective))
            cancellation_token.link_future(effect_task)
            return await effect_task

        tool = sdk["FunctionTool"](
            sentinel_write,
            name=self._actions[0].tool_name,
            description="Commit a harmless AgentBarrier sentinel effect.",
        )
        runtime = sdk["SingleThreadedAgentRuntime"](
            intervention_handlers=[ApprovalInterventionHandler()]
        )
        tool_agent_type = await sdk["ToolAgent"].register(
            runtime,
            "agentbarrier_tool_executor",
            lambda: sdk["ToolAgent"](
                description="Execute AgentBarrier sentinel tools.",
                tools=[tool],
            ),
        )
        runtime.start()
        tokens = [sdk["CancellationToken"]() for _action in self._actions]
        tasks: list[asyncio.Task[Any]] = []
        try:
            for action, token in zip(self._actions, tokens, strict=True):
                call = sdk["FunctionCall"](
                    id=action.action_id,
                    name=action.tool_name,
                    arguments=json.dumps(
                        {"action_id": action.action_id, **dict(action.arguments)},
                        sort_keys=True,
                    ),
                )
                tasks.append(
                    asyncio.create_task(
                        runtime.send_message(
                            call,
                            sdk["AgentId"](tool_agent_type, self._run_id),
                            cancellation_token=token,
                        )
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._validate_results(results, sdk)
        finally:
            for future in self._decisions.values():
                if not future.done():
                    future.cancel()
            for token in tokens:
                token.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await runtime.stop()

    async def _intercept(self, message: Any, sdk: dict[str, Any]) -> Any:
        action_id = str(message.id)
        original = self._action_by_id.get(action_id)
        if original is None:
            raise sdk["ToolException"](
                call_id=action_id,
                content=f"Unknown AgentBarrier action id {action_id!r}",
                name=str(message.name),
            )
        if message.name != original.tool_name:
            raise sdk["ToolException"](
                call_id=action_id,
                content=f"AutoGen changed the tool name for action {action_id!r}",
                name=str(message.name),
            )
        arguments = _decode_arguments(message.arguments)
        returned_action_id = arguments.pop("action_id", None)
        if returned_action_id != action_id:
            raise sdk["ToolException"](
                call_id=action_id,
                content=f"AutoGen changed the logical action id for {action_id!r}",
                name=str(message.name),
            )
        if not original.requires_approval:
            return message
        if action_id in self._decisions:
            raise sdk["ToolException"](
                call_id=action_id,
                content=f"AutoGen returned duplicate approval for {action_id!r}",
                name=str(message.name),
            )

        pending = original.with_arguments(_json_mapping(arguments))
        self._pending_by_id[action_id] = pending
        self._decisions[action_id] = asyncio.get_running_loop().create_future()
        if len(self._pending_by_id) == self._expected_pending:
            self._pending = tuple(
                self._pending_by_id[action.action_id]
                for action in self._actions
                if action.action_id in self._pending_by_id
            )
            self._pending_ready.set()

        decision, replacement, reason = await self._decisions[action_id]
        if decision is Decision.REJECT:
            self._rejected.add(action_id)
            raise sdk["ToolException"](
                call_id=action_id,
                content=reason or "User denied tool execution.",
                name=original.tool_name,
            )
        if replacement is None:
            return message
        return sdk["FunctionCall"](
            id=action_id,
            name=original.tool_name,
            arguments=json.dumps(
                {"action_id": action_id, **dict(replacement.arguments)},
                sort_keys=True,
            ),
        )

    def _validate_results(self, results: list[Any], sdk: dict[str, Any]) -> None:
        for action, result in zip(self._actions, results, strict=True):
            if action.action_id in self._rejected:
                if not isinstance(result, sdk["ToolException"]):
                    raise AdapterContractError(
                        f"AutoGen did not return a rejection for {action.action_id!r}"
                    )
                continue
            if isinstance(result, BaseException):
                raise AdapterContractError(
                    f"AutoGen tool execution failed for {action.action_id!r}: "
                    f"{type(result).__name__}: {result}"
                )
            if not isinstance(result, sdk["FunctionExecutionResult"]):
                raise AdapterContractError(
                    f"AutoGen returned an unexpected result for {action.action_id!r}"
                )
            if result.call_id != action.action_id or result.name != action.tool_name:
                raise AdapterContractError(
                    f"AutoGen changed result identity for {action.action_id!r}"
                )
            if result.is_error:
                raise AdapterContractError(
                    f"AutoGen reported an error result for {action.action_id!r}: {result.content}"
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
            pending = self._pending_by_id.get(action_id)
            if pending is None:
                raise AdapterContractError(f"{action_id!r} is not pending approval")
            if replacement.action_id != action_id:
                raise AdapterContractError("replacement must preserve action_id")
            if replacement.tool_name != pending.tool_name:
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
            return RunOutcome(RunStatus.FAILED, "AutoGen run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("AutoGen replay is not declared by this adapter")

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()


def _decode_arguments(raw: str) -> dict[Any, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterContractError("AutoGen returned invalid JSON tool arguments") from exc
    if not isinstance(value, dict):
        raise AdapterContractError("AutoGen returned non-object tool arguments")
    return value


def _json_mapping(value: dict[Any, Any]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AdapterContractError("action argument keys must be strings")
        result[key] = item
    return result


def _load_sdk() -> dict[str, Any]:
    try:
        from autogen_core import (
            AgentId,
            CancellationToken,
            DefaultInterventionHandler,
            FunctionCall,
            SingleThreadedAgentRuntime,
        )
        from autogen_core.models import FunctionExecutionResult
        from autogen_core.tool_agent import ToolAgent, ToolException
        from autogen_core.tools import FunctionTool
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "AutoGenAdapter requires `pip install agentbarrier[autogen]`"
        ) from exc
    return {
        "AgentId": AgentId,
        "CancellationToken": CancellationToken,
        "DefaultInterventionHandler": DefaultInterventionHandler,
        "FunctionCall": FunctionCall,
        "FunctionExecutionResult": FunctionExecutionResult,
        "FunctionTool": FunctionTool,
        "SingleThreadedAgentRuntime": SingleThreadedAgentRuntime,
        "ToolAgent": ToolAgent,
        "ToolException": ToolException,
    }
