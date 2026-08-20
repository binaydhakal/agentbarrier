"""Controlled sentinel effect used by lifecycle scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agentbarrier.errors import AmbiguousEffectError
from agentbarrier.journal import EffectJournal
from agentbarrier.models import (
    ActionRequest,
    EffectPhase,
    ReconciliationEvidence,
    ReconciliationStatus,
    action_digest,
)


class EffectProbe:
    """Records a real external effect and can pause immediately before commit."""

    def __init__(
        self,
        journal: EffectJournal,
        *,
        run_id: str,
        block_before_commit: bool = False,
        raise_before_commit: bool = False,
        raise_after_commit: bool = False,
        reconciliation_available: bool = True,
        reconciliation_delay_seconds: float = 0.0,
        commit_action: Callable[[ActionRequest], bool | None] | None = None,
        reconcile_action: Callable[[ActionRequest], ReconciliationEvidence] | None = None,
    ) -> None:
        if reconciliation_delay_seconds < 0:
            raise ValueError("reconciliation_delay_seconds must not be negative")
        self.journal = journal
        self.run_id = run_id
        self.block_before_commit = block_before_commit
        self.reconciliation_available = reconciliation_available
        self.reconciliation_delay_seconds = reconciliation_delay_seconds
        self.commit_action = commit_action
        self.reconcile_action = reconcile_action
        self._raise_before_commit_remaining = int(raise_before_commit)
        self._raise_after_commit_remaining = int(raise_after_commit)
        self._started = asyncio.Event()
        self._release = asyncio.Event()
        if not block_before_commit:
            self._release.set()

    async def __call__(self, action: ActionRequest) -> str:
        """Execute the harmless sentinel effect for one action."""

        arguments = dict(action.arguments)
        self.journal.record(
            run_id=self.run_id,
            action_id=action.action_id,
            tool_name=action.tool_name,
            phase=EffectPhase.STARTED,
            arguments=arguments,
        )
        self._started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.journal.record(
                run_id=self.run_id,
                action_id=action.action_id,
                tool_name=action.tool_name,
                phase=EffectPhase.ABORTED,
                arguments=arguments,
                detail="cancelled before commit",
            )
            raise
        if self._raise_before_commit_remaining:
            self._raise_before_commit_remaining -= 1
            raise AmbiguousEffectError(action.action_id, commit_observed=False)
        committed = self.commit_action(action) if self.commit_action is not None else True
        if committed is not False:
            self.journal.record(
                run_id=self.run_id,
                action_id=action.action_id,
                tool_name=action.tool_name,
                phase=EffectPhase.COMMITTED,
                arguments=arguments,
            )
        else:
            return f"sentinel effect already committed for {action.action_id}"
        if self._raise_after_commit_remaining:
            self._raise_after_commit_remaining -= 1
            raise AmbiguousEffectError(action.action_id)
        return f"sentinel effect committed for {action.action_id}"

    async def reconcile(self, action: ActionRequest) -> ReconciliationEvidence:
        """Query the durable sentinel journal by stable action identity."""

        expected_digest = action_digest(action)
        if self.reconciliation_delay_seconds:
            await asyncio.sleep(self.reconciliation_delay_seconds)
        if not self.reconciliation_available:
            return ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.UNAVAILABLE,
                expected_action_digest=expected_digest,
                detail="reconciliation evidence is unavailable",
            )
        if self.reconcile_action is not None:
            return self.reconcile_action(action)
        commits = tuple(
            event
            for event in self.journal.committed(run_id=self.run_id)
            if event.action_id == action.action_id
        )
        observed_digests = tuple(
            action_digest(
                ActionRequest(
                    action_id=event.action_id,
                    tool_name=event.tool_name,
                    arguments=event.arguments,
                    requires_approval=action.requires_approval,
                    parent_action_id=action.parent_action_id,
                )
            )
            for event in commits
        )
        if not commits:
            return ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.NOT_COMMITTED,
                expected_action_digest=expected_digest,
                detail="no committed effect exists for the stable action identity",
            )
        if len(commits) == 1 and observed_digests == (expected_digest,):
            return ReconciliationEvidence(
                action_id=action.action_id,
                status=ReconciliationStatus.COMMITTED,
                expected_action_digest=expected_digest,
                observed_action_digests=observed_digests,
                detail="one matching committed effect was found",
            )
        return ReconciliationEvidence(
            action_id=action.action_id,
            status=ReconciliationStatus.CONFLICT,
            expected_action_digest=expected_digest,
            observed_action_digests=observed_digests,
            detail=f"found {len(commits)} conflicting commit record(s)",
        )

    async def wait_started(self, timeout_seconds: float) -> None:
        """Wait until at least one invocation crosses the execution boundary."""

        await asyncio.wait_for(self._started.wait(), timeout=timeout_seconds)

    def release(self) -> None:
        """Allow blocked invocations to attempt their commit."""

        self._release.set()
