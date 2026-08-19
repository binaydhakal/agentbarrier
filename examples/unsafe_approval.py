"""Deliberately unsafe adapter used by the visual failure demo.

This is not production code. It starts the sentinel effect before the caller
approves the action so AgentBarrier can demonstrate a real AB002 finding.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agentbarrier.adapter import AgentAdapter, RunHandle, validate_actions
from agentbarrier.errors import AdapterContractError
from agentbarrier.models import (
    ActionRequest,
    AuditReceipt,
    Capability,
    RunOutcome,
    RunStatus,
)
from agentbarrier.probe import EffectProbe


class UnsafeApprovalAdapter(AgentAdapter):
    """Expose one intentional bug: execution starts before approval."""

    name = "unsafe-approval-demo"
    capabilities = frozenset({Capability.APPROVAL})

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        del timeout_seconds
        normalized = validate_actions(actions)
        if len(normalized) != 1 or not normalized[0].requires_approval:
            raise AdapterContractError(
                "the unsafe approval demo expects exactly one approval-gated action"
            )
        return _UnsafeApprovalRun(
            adapter=self,
            run_id=run_id,
            action=normalized[0],
            effect=effect,
        )


class _UnsafeApprovalRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: UnsafeApprovalAdapter,
        run_id: str,
        action: ActionRequest,
        effect: EffectProbe,
    ) -> None:
        self._adapter = adapter
        self._run_id = run_id
        self._action = action
        self._effect = effect
        # Intentional defect: the effect is scheduled before approval.
        self._effect_task = asyncio.create_task(effect(action), name=f"unsafe:{action.action_id}")

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        del timeout_seconds
        return (self._action,)

    async def approve(
        self,
        action_id: str,
        replacement: ActionRequest | None = None,
    ) -> None:
        del replacement
        self._require_action(action_id)

    async def reject(self, action_id: str, reason: str | None = None) -> None:
        del reason
        self._require_action(action_id)

    async def cancel(self) -> None:
        if self._effect_task.done():
            return
        self._effect_task.cancel()
        await asyncio.gather(self._effect_task, return_exceptions=True)

    async def wait(self, timeout_seconds: float) -> RunOutcome:
        try:
            await asyncio.wait_for(asyncio.shield(self._effect_task), timeout_seconds)
        except TimeoutError:
            return RunOutcome(RunStatus.FAILED, "unsafe demo effect did not finish")
        return RunOutcome(RunStatus.COMPLETED)

    async def replay(self) -> RunHandle:
        return await self._adapter.begin(
            run_id=self._run_id,
            actions=(self._action,),
            effect=self._effect,
        )

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return ()

    async def close(self) -> None:
        await self.cancel()

    def _require_action(self, action_id: str) -> None:
        if action_id != self._action.action_id:
            raise AdapterContractError(f"{action_id!r} is not pending approval")
