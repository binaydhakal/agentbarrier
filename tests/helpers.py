"""Deliberately unsafe adapters used to verify finding detection."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agentbarrier.adapter import AgentAdapter, RunHandle
from agentbarrier.errors import AmbiguousEffectError
from agentbarrier.models import (
    ActionRequest,
    AuditEvent,
    AuditReceipt,
    Capability,
    RunOutcome,
    RunStatus,
    action_digest,
)
from agentbarrier.probe import EffectProbe


class UnsafeAdapter(AgentAdapter):
    """Exercise exactly one known failure mode."""

    def __init__(self, mode: str, capability: Capability) -> None:
        self.mode = mode
        self.name = f"unsafe-{mode}"
        self.capabilities = frozenset({capability})

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        return UnsafeRun(
            adapter=self,
            run_id=run_id,
            actions=tuple(actions),
            effect=effect,
            timeout_seconds=timeout_seconds,
        )


class UnsafeRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: UnsafeAdapter,
        run_id: str,
        actions: tuple[ActionRequest, ...],
        effect: EffectProbe,
        timeout_seconds: float | None,
    ) -> None:
        self.adapter = adapter
        self.run_id = run_id
        self.actions = actions
        self.effect = effect
        self.timeout_seconds = timeout_seconds
        self.pending = tuple(item for item in actions if item.requires_approval)
        self.tasks: list[asyncio.Task[str]] = []

        if adapter.mode in {
            "early",
            "rejection",
            "replay",
            "cancellation",
            "timeout",
            "ambiguity",
        }:
            self.tasks.append(asyncio.create_task(effect(actions[0])))
        elif adapter.mode == "parallel":
            sibling = next(item for item in actions if not item.requires_approval)
            self.tasks.append(asyncio.create_task(effect(sibling)))
        elif adapter.mode == "delegation":
            child = next(item for item in actions if item.parent_action_id is not None)
            self.tasks.append(asyncio.create_task(effect(child)))

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        del timeout_seconds
        return self.pending

    async def approve(
        self,
        action_id: str,
        replacement: ActionRequest | None = None,
    ) -> None:
        original = next(item for item in self.actions if item.action_id == action_id)
        if self.adapter.mode in {"binding", "audit_wrong_run"}:
            self.tasks.append(asyncio.create_task(self.effect(original)))
        elif self.adapter.mode == "parallel":
            self.tasks.append(asyncio.create_task(self.effect(replacement or original)))

    async def reject(self, action_id: str, reason: str | None = None) -> None:
        del action_id, reason

    async def cancel(self) -> None:
        # Deliberately leave child tasks alive.
        return None

    async def wait(self, timeout_seconds: float) -> RunOutcome:
        if self.adapter.mode == "timeout":
            return RunOutcome(RunStatus.TIMED_OUT)
        if self.tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=timeout_seconds)
            except AmbiguousEffectError as exc:
                return RunOutcome(RunStatus.UNKNOWN, str(exc))
        return RunOutcome(RunStatus.COMPLETED)

    async def replay(self) -> RunHandle:
        return await self.adapter.begin(
            run_id=self.run_id,
            actions=self.actions,
            effect=self.effect,
            timeout_seconds=self.timeout_seconds,
        )

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        if self.adapter.mode == "audit_wrong_run":
            approved, rejected = self.actions
            wrong_run = f"wrong-{self.run_id}"
            raw = (
                (AuditEvent.RUN_STARTED, None, None),
                (
                    AuditEvent.APPROVAL_REQUESTED,
                    approved.action_id,
                    action_digest(approved),
                ),
                (
                    AuditEvent.APPROVAL_REQUESTED,
                    rejected.action_id,
                    action_digest(rejected),
                ),
                (AuditEvent.APPROVED, approved.action_id, action_digest(approved)),
                (AuditEvent.REJECTED, rejected.action_id, action_digest(rejected)),
                (AuditEvent.RUN_COMPLETED, None, None),
            )
            return tuple(
                AuditReceipt(
                    sequence=index,
                    run_id=wrong_run,
                    event=event,
                    timestamp_ns=index,
                    action_id=action_id,
                    action_digest=digest,
                )
                for index, (event, action_id, digest) in enumerate(raw, start=1)
            )
        return ()

    async def close(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class EmptyCapabilityAdapter(AgentAdapter):
    name = "empty"
    capabilities = frozenset[Capability]()

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        del run_id, actions, effect, timeout_seconds
        raise AssertionError("begin must not be called for unsupported scenarios")
