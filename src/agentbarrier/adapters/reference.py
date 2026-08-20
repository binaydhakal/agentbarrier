"""Safe reference implementation of the AgentBarrier adapter contract."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agentbarrier.adapter import AgentAdapter, RunHandle, validate_actions, validate_timeout
from agentbarrier.errors import AdapterContractError, AmbiguousEffectError
from agentbarrier.models import (
    ActionRequest,
    AuditEvent,
    AuditReceipt,
    Capability,
    Decision,
    ReconciliationEvidence,
    ReconciliationStatus,
    RunOutcome,
    RunStatus,
    action_digest,
)
from agentbarrier.probe import EffectProbe

_DecisionValue = tuple[Decision, ActionRequest | None]


class ReferenceAdapter(AgentAdapter):
    """A correct model-free adapter used to validate the harness itself."""

    name = "reference"
    capabilities = frozenset(Capability)

    def __init__(self) -> None:
        self._completed: set[tuple[str, str]] = set()

    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        normalized = validate_actions(actions)
        return _ReferenceRun(
            adapter=self,
            run_id=run_id,
            actions=normalized,
            effect=effect,
            timeout_seconds=validate_timeout(timeout_seconds),
        )


class _ReferenceRun(RunHandle):
    def __init__(
        self,
        *,
        adapter: ReferenceAdapter,
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
        self._pending = tuple(action for action in actions if action.requires_approval)
        self._pending_ready = asyncio.Event()
        self._decisions: dict[str, asyncio.Future[_DecisionValue]] = {}
        self._reconciliations: dict[str, ReconciliationEvidence] = {}
        self._terminal: RunOutcome | None = None
        self._effect.journal.record_receipt(run_id=run_id, event=AuditEvent.RUN_STARTED)
        self._task = asyncio.create_task(self._drive(), name=f"agentbarrier:{run_id}")

    async def _drive(self) -> RunOutcome:
        try:
            if self._timeout_seconds is None:
                await self._run_lifecycle()
            else:
                await asyncio.wait_for(self._run_lifecycle(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            self._terminal = RunOutcome(RunStatus.TIMED_OUT, "run exceeded its tool timeout")
            self._record_run_event(AuditEvent.RUN_TIMED_OUT)
        except asyncio.CancelledError:
            self._terminal = RunOutcome(RunStatus.CANCELLED, "run was cancelled")
            self._record_run_event(AuditEvent.RUN_CANCELLED)
        except AmbiguousEffectError as exc:
            self._terminal = RunOutcome(RunStatus.UNKNOWN, str(exc))
            self._record_run_event(AuditEvent.RUN_UNKNOWN, detail=str(exc))
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            self._terminal = RunOutcome(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
            self._record_run_event(AuditEvent.RUN_FAILED, detail=self._terminal.detail)
        else:
            self._terminal = RunOutcome(RunStatus.COMPLETED)
            self._record_run_event(AuditEvent.RUN_COMPLETED)
        return self._terminal

    async def _run_lifecycle(self) -> None:
        executable = await self._resolve_approvals()
        await self._execute_all(executable)

    async def _resolve_approvals(self) -> tuple[ActionRequest, ...]:
        if not self._pending:
            return self._actions

        loop = asyncio.get_running_loop()
        for action in self._pending:
            self._decisions[action.action_id] = loop.create_future()
            self._record_action_event(AuditEvent.APPROVAL_REQUESTED, action)
        self._pending_ready.set()

        decisions = {
            action.action_id: await self._decisions[action.action_id] for action in self._pending
        }
        action_by_id = {action.action_id: action for action in self._actions}
        executable: list[ActionRequest] = []
        for action in self._actions:
            if not self._ancestors_allowed(action, action_by_id, decisions):
                continue
            if not action.requires_approval:
                executable.append(action)
                continue
            decision, replacement = decisions[action.action_id]
            if decision is Decision.APPROVE:
                executable.append(replacement or action)
        return tuple(executable)

    @staticmethod
    def _ancestors_allowed(
        action: ActionRequest,
        action_by_id: dict[str, ActionRequest],
        decisions: dict[str, _DecisionValue],
    ) -> bool:
        parent_id = action.parent_action_id
        while parent_id is not None:
            parent = action_by_id[parent_id]
            if parent.requires_approval:
                decision, _ = decisions[parent.action_id]
                if decision is not Decision.APPROVE:
                    return False
            parent_id = parent.parent_action_id
        return True

    async def _execute_all(self, actions: Sequence[ActionRequest]) -> None:
        tasks = [
            asyncio.create_task(self._execute_one(action), name=f"effect:{action.action_id}")
            for action in actions
            if (self._run_id, action.action_id) not in self._adapter._completed
        ]
        if tasks:
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    async def _execute_one(self, action: ActionRequest) -> None:
        try:
            await self._effect(action)
        except AmbiguousEffectError:
            self._adapter._completed.add((self._run_id, action.action_id))
            raise
        else:
            self._adapter._completed.add((self._run_id, action.action_id))

    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        if not self._pending:
            raise AdapterContractError("this run contains no approval-gated actions")
        await asyncio.wait_for(self._pending_ready.wait(), timeout=timeout_seconds)
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
        future = self._decision_future(action_id)
        original = next((action for action in self._pending if action.action_id == action_id), None)
        if original is None:  # pragma: no cover - future lookup guarantees the same identity
            raise AdapterContractError(f"{action_id!r} is not pending approval")
        if replacement is not None:
            if replacement.action_id != original.action_id:
                raise AdapterContractError("replacement must preserve action_id")
            if replacement.tool_name != original.tool_name:
                raise AdapterContractError("replacement must preserve tool_name")
        approved = replacement or original
        self._record_action_event(AuditEvent.APPROVED, approved)
        future.set_result((Decision.APPROVE, replacement))

    async def reject(self, action_id: str, reason: str | None = None) -> None:
        future = self._decision_future(action_id)
        original = next((action for action in self._pending if action.action_id == action_id), None)
        if original is None:
            raise AdapterContractError(f"{action_id!r} is not pending approval")
        self._record_action_event(AuditEvent.REJECTED, original, detail=reason)
        future.set_result((Decision.REJECT, None))

    async def cancel(self) -> None:
        if self._task.done():
            return
        self._task.cancel()
        await self._task

    async def wait(self, timeout_seconds: float) -> RunOutcome:
        try:
            return await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return RunOutcome(RunStatus.FAILED, "run did not reach a terminal state in time")

    async def reconcile(
        self,
        action_id: str,
        timeout_seconds: float,
    ) -> ReconciliationEvidence:
        cached = self._reconciliations.get(action_id)
        if cached is not None:
            return cached
        if not self._task.done() or self._terminal is None:
            raise AdapterContractError("cannot reconcile before the run reaches a terminal state")
        if self._terminal.status is not RunStatus.UNKNOWN:
            raise AdapterContractError("only an unknown run outcome can be reconciled")
        action = next((item for item in self._actions if item.action_id == action_id), None)
        if action is None:
            raise AdapterContractError(f"{action_id!r} does not identify an action in this run")
        bounded_timeout = validate_timeout(timeout_seconds)
        if bounded_timeout is None:  # pragma: no cover - the public argument is not optional
            raise AdapterContractError("reconciliation timeout is required")

        expected_digest = action_digest(action)
        self._record_action_event(AuditEvent.RECONCILIATION_STARTED, action)
        try:
            evidence = await asyncio.wait_for(
                self._effect.reconcile(action),
                timeout=bounded_timeout,
            )
        except asyncio.TimeoutError:
            evidence = ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.UNAVAILABLE,
                expected_action_digest=expected_digest,
                detail=f"reconciliation timed out after {bounded_timeout:g} seconds",
            )
            event = AuditEvent.RECONCILIATION_TIMED_OUT
        else:
            if (
                evidence.action_id != action.action_id
                or evidence.expected_action_digest != expected_digest
            ):
                evidence = ReconciliationEvidence(
                    action_id=action.action_id,
                    status=ReconciliationStatus.CONFLICT,
                    expected_action_digest=expected_digest,
                    observed_action_digests=evidence.observed_action_digests,
                    detail="reconciliation evidence was bound to a different action identity",
                )
            event = {
                ReconciliationStatus.COMMITTED: AuditEvent.RECONCILIATION_COMMITTED,
                ReconciliationStatus.NOT_COMMITTED: AuditEvent.RECONCILIATION_NOT_COMMITTED,
                ReconciliationStatus.CONFLICT: AuditEvent.RECONCILIATION_CONFLICT,
                ReconciliationStatus.UNAVAILABLE: AuditEvent.RECONCILIATION_UNAVAILABLE,
            }[evidence.status]

        if evidence.status is ReconciliationStatus.COMMITTED:
            self._adapter._completed.add((self._run_id, action.action_id))
        elif evidence.status is ReconciliationStatus.NOT_COMMITTED:
            self._adapter._completed.discard((self._run_id, action.action_id))
        self._reconciliations[action.action_id] = evidence
        self._record_action_event(event, action, detail=evidence.detail)
        return evidence

    async def replay(self) -> RunHandle:
        if not self._task.done():
            raise AdapterContractError("cannot replay a run before it reaches a terminal state")
        return await self._adapter.begin(
            run_id=self._run_id,
            actions=self._actions,
            effect=self._effect,
            timeout_seconds=self._timeout_seconds,
        )

    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        return self._effect.journal.receipts(run_id=self._run_id)

    async def close(self) -> None:
        await self.cancel()

    def _record_action_event(
        self,
        event: AuditEvent,
        action: ActionRequest,
        *,
        detail: str | None = None,
    ) -> None:
        self._effect.journal.record_receipt(
            run_id=self._run_id,
            event=event,
            action_id=action.action_id,
            action_digest=action_digest(action),
            detail=detail,
        )

    def _record_run_event(self, event: AuditEvent, *, detail: str | None = None) -> None:
        self._effect.journal.record_receipt(
            run_id=self._run_id,
            event=event,
            detail=detail,
        )
