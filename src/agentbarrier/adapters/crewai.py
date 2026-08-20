"""Credential-free conformance adapter for CrewAI."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from tempfile import TemporaryDirectory
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
_TELEMETRY_LOCK = threading.Lock()


class CrewAIAdapter(AgentAdapter):
    """Exercise CrewAI's real pre-tool hook with a deterministic local model.

    CrewAI runs native tool calls in worker threads. Its synchronous pre-tool hook can block a
    call or edit its arguments in place, which is sufficient for approval, rejection, argument
    binding, and per-action parallel barriers. CrewAI does not expose a safe way to terminate an
    already-running tool thread, so this adapter intentionally does not claim cancellation or
    timeout fencing.
    """

    capabilities = frozenset(
        {
            Capability.APPROVAL,
            Capability.REJECTION,
            Capability.ARGUMENT_BINDING,
            Capability.PARALLEL_BARRIER,
        }
    )

    def __init__(self) -> None:
        try:
            sdk_version = version("crewai")
        except PackageNotFoundError:
            sdk_version = "not-installed"
        self.name = f"crewai/{sdk_version}"

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
            raise AdapterContractError("CrewAIAdapter requires one shared tool_name per run")
        for action in normalized:
            _validated_sentinel_arguments(action.arguments)
        with _telemetry_disabled():
            _load_sdk()
        return _CrewAIRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _CrewAIRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: CrewAIAdapter,
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
        self._action_by_id = {action.action_id: action for action in actions}
        self._required_ids = {action.action_id for action in actions if action.requires_approval}
        self._loop = asyncio.get_running_loop()
        self._pending_ready = asyncio.Event()
        self._pending: dict[str, ActionRequest] = {}
        self._pending_snapshot: tuple[ActionRequest, ...] = ()
        self._decisions: dict[str, _DecisionValue] = {}
        self._condition = threading.Condition()
        self._effect_futures: set[concurrent.futures.Future[str]] = set()
        self._stop_status: RunStatus | None = None
        self._thread_error: BaseException | None = None
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:crewai:{run_id}")

    async def _drive(self) -> RunOutcome:
        worker = asyncio.create_task(asyncio.to_thread(self._run_crewai))
        try:
            if self._timeout_seconds is None:
                await worker
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker),
                        timeout=self._timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    self._request_stop(RunStatus.TIMED_OUT)
                    await worker
        except asyncio.CancelledError:
            self._request_stop(RunStatus.CANCELLED)
            return RunOutcome(RunStatus.CANCELLED, "CrewAI run was cancelled")
        except Exception as exc:
            return RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")

        with self._condition:
            stop_status = self._stop_status
            thread_error = self._thread_error
        if thread_error is not None:
            return RunOutcome(
                RunStatus.FAILED,
                f"{type(thread_error).__name__}: {thread_error}",
            )
        if stop_status is RunStatus.TIMED_OUT:
            return RunOutcome(RunStatus.TIMED_OUT, "CrewAI run exceeded its timeout")
        if stop_status is RunStatus.CANCELLED:
            return RunOutcome(RunStatus.CANCELLED, "CrewAI run was cancelled")
        return RunOutcome(RunStatus.COMPLETED)

    def _run_crewai(self) -> None:
        with (
            TemporaryDirectory(prefix="agentbarrier-crewai-") as storage_directory,
            _telemetry_disabled(storage_directory=storage_directory),
        ):
            sdk = _load_sdk()
            run = self
            base_tool = sdk["BaseTool"]
            base_llm = sdk["BaseLLM"]

            class SentinelTool(base_tool):  # type: ignore[misc, valid-type]
                name: str = "sentinel_write"
                description: str = "Commit a harmless AgentBarrier sentinel effect."

                def _run(self, action_id: str, recipient: str, amount: int) -> str:
                    return run._execute_effect(
                        action_id,
                        {"recipient": recipient, "amount": amount},
                    )

            class ScriptedLLM(base_llm):  # type: ignore[misc, valid-type]
                def __init__(self) -> None:
                    super().__init__(model="agentbarrier-local", provider="agentbarrier")

                def supports_function_calling(self) -> bool:
                    return True

                def call(
                    self,
                    messages: Any,
                    tools: Any = None,
                    callbacks: Any = None,
                    available_functions: Any = None,
                    from_task: Any = None,
                    from_agent: Any = None,
                    response_model: Any = None,
                ) -> str | list[dict[str, Any]]:
                    del tools, callbacks, available_functions, from_task, from_agent, response_model
                    if any(
                        isinstance(message, dict) and message.get("role") == "tool"
                        for message in messages
                    ):
                        return "All deterministic sentinel actions were processed."
                    return [
                        {
                            "id": action.action_id,
                            "type": "function",
                            "function": {
                                "name": action.tool_name,
                                "arguments": json.dumps(
                                    {
                                        "action_id": action.action_id,
                                        **dict(action.arguments),
                                    },
                                    sort_keys=True,
                                ),
                            },
                        }
                        for action in run._actions
                    ]

            tool = SentinelTool(name=self._actions[0].tool_name)
            model = ScriptedLLM()
            agent = sdk["Agent"](
                role="AgentBarrier conformance probe",
                goal="Execute every supplied sentinel action exactly once.",
                backstory="A deterministic local lifecycle probe.",
                llm=model,
                tools=[tool],
                allow_delegation=False,
                max_iter=3,
                verbose=False,
            )
            task = sdk["Task"](
                description="Execute the deterministic sentinel actions.",
                expected_output="A confirmation that every action was processed.",
                agent=agent,
            )
            crew = sdk["Crew"](
                agents=[agent],
                tasks=[task],
                cache=False,
                memory=False,
                planning=False,
                share_crew=False,
                verbose=False,
            )

            def approval_hook(context: Any) -> bool | None:
                if context.agent is not agent:
                    return None
                return self._intercept_tool_call(context.tool_name, context.tool_input)

            sdk["register_before_tool_call_hook"](approval_hook)
            try:
                crew.kickoff()
            finally:
                sdk["unregister_before_tool_call_hook"](approval_hook)

    def _intercept_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool | None:
        action_id = str(tool_input.get("action_id", ""))
        original = self._action_by_id.get(action_id)
        if original is None or tool_name != (original.tool_name if original else ""):
            self._record_thread_error(
                AdapterContractError(
                    f"CrewAI invoked an unknown action {action_id!r} through tool {tool_name!r}"
                )
            )
            return False
        try:
            proposed_arguments = _tool_arguments(tool_input)
            proposed = original.with_arguments(proposed_arguments)
        except (TypeError, ValueError, AdapterContractError) as exc:
            self._record_thread_error(exc)
            return False

        with self._condition:
            if self._stop_status is not None or self._thread_error is not None:
                return False
            if not original.requires_approval:
                return None
            if action_id in self._pending:
                self._thread_error = AdapterContractError(
                    f"CrewAI requested duplicate approval for {action_id!r}"
                )
                self._condition.notify_all()
                return False

            self._pending[action_id] = proposed
            if self._pending.keys() == self._required_ids:
                self._pending_snapshot = tuple(
                    self._pending[action.action_id]
                    for action in self._actions
                    if action.action_id in self._pending
                )
                self._loop.call_soon_threadsafe(self._pending_ready.set)

            while (
                action_id not in self._decisions
                and self._stop_status is None
                and self._thread_error is None
            ):
                self._condition.wait()

            if self._stop_status is not None or self._thread_error is not None:
                return False
            decision, replacement, _reason = self._decisions[action_id]
            if decision is Decision.REJECT:
                return False
            effective = replacement or proposed
            tool_input.clear()
            tool_input.update({"action_id": action_id, **dict(effective.arguments)})
            return None

    def _execute_effect(self, action_id: str, arguments: dict[str, JsonValue]) -> str:
        original = self._action_by_id.get(action_id)
        if original is None:
            raise AdapterContractError(f"CrewAI invoked unknown action id {action_id!r}")
        effective = original.with_arguments(arguments)
        with self._condition:
            if self._stop_status is not None or self._thread_error is not None:
                raise AgentBarrierError("CrewAI run stopped before the effect boundary")
            future = asyncio.run_coroutine_threadsafe(self._effect(effective), self._loop)
            self._effect_futures.add(future)
        try:
            return future.result()
        finally:
            with self._condition:
                self._effect_futures.discard(future)

    def _record_thread_error(self, error: BaseException) -> None:
        with self._condition:
            if self._thread_error is None:
                self._thread_error = error
            self._condition.notify_all()

    def _request_stop(self, status: RunStatus) -> None:
        with self._condition:
            if self._stop_status is None:
                self._stop_status = status
            futures = tuple(self._effect_futures)
            self._condition.notify_all()
        for future in futures:
            future.cancel()

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        await wait_for_pending_or_terminal(
            pending_event=self._pending_ready,
            run_task=self._task,
            timeout_seconds=timeout_seconds,
            adapter_name=self._adapter.name,
        )
        return self._pending_snapshot

    def _set_decision(
        self,
        action_id: str,
        decision: Decision,
        replacement: ActionRequest | None,
        reason: str | None,
    ) -> None:
        with self._condition:
            original = self._pending.get(action_id)
            if original is None:
                raise AdapterContractError(f"{action_id!r} is not pending approval")
            if action_id in self._decisions:
                raise AdapterContractError(f"{action_id!r} already has a decision")
            if replacement is not None:
                if replacement.action_id != original.action_id:
                    raise AdapterContractError("replacement must preserve action_id")
                if replacement.tool_name != original.tool_name:
                    raise AdapterContractError("replacement must preserve tool_name")
                _validated_sentinel_arguments(replacement.arguments)
            self._decisions[action_id] = (decision, replacement, reason)
            self._condition.notify_all()

    async def approve(
        self,
        action_id: str,
        replacement: ActionRequest | None = None,
    ) -> None:
        self._set_decision(action_id, Decision.APPROVE, replacement, None)

    async def reject(self, action_id: str, reason: str | None = None) -> None:
        self._set_decision(action_id, Decision.REJECT, None, reason)

    async def cancel(self) -> None:
        if self._task.done():
            return
        self._request_stop(RunStatus.CANCELLED)
        await self._task

    async def wait(self, timeout_seconds: float) -> RunOutcome:
        try:
            return await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.FAILED, "CrewAI run did not terminate in time")

    async def replay(self) -> RunHandle:
        raise UnsupportedCapability("CrewAI replay is not declared by this adapter")

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()


def _validated_sentinel_arguments(arguments: Any) -> dict[str, JsonValue]:
    values = dict(arguments)
    if set(values) != {"recipient", "amount"}:
        raise AdapterContractError(
            "CrewAIAdapter sentinel actions require exactly 'recipient' and 'amount' arguments"
        )
    if not isinstance(values["recipient"], str):
        raise AdapterContractError("CrewAIAdapter 'recipient' must be a string")
    amount = values["amount"]
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise AdapterContractError("CrewAIAdapter 'amount' must be an integer")
    return {"recipient": values["recipient"], "amount": amount}


def _tool_arguments(tool_input: dict[str, Any]) -> dict[str, JsonValue]:
    values = dict(tool_input)
    values.pop("action_id", None)
    return _validated_sentinel_arguments(values)


@contextmanager
def _telemetry_disabled(*, storage_directory: str | None = None) -> Iterator[None]:
    with _TELEMETRY_LOCK:
        values = {
            "OTEL_SDK_DISABLED": "true",
            "CREWAI_DISABLE_TELEMETRY": "true",
        }
        if storage_directory is not None:
            values["CREWAI_STORAGE_DIR"] = storage_directory
        names = tuple(values)
        previous = {name: os.environ.get(name) for name in names}
        os.environ.update(values)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _load_sdk() -> dict[str, Any]:
    try:
        from crewai import Agent, Crew, Task  # type: ignore[import-not-found]
        from crewai.hooks import (  # type: ignore[import-not-found]
            register_before_tool_call_hook,
            unregister_before_tool_call_hook,
        )
        from crewai.llms.base_llm import BaseLLM  # type: ignore[import-not-found]
        from crewai.tools import BaseTool  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised in clean optional-dependency test
        raise AgentBarrierError(
            "CrewAIAdapter requires `pip install agentbarrier[crewai]`"
        ) from exc
    return {
        "Agent": Agent,
        "BaseLLM": BaseLLM,
        "BaseTool": BaseTool,
        "Crew": Crew,
        "Task": Task,
        "register_before_tool_call_hook": register_before_tool_call_hook,
        "unregister_before_tool_call_hook": unregister_before_tool_call_hook,
    }
