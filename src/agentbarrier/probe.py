"""Controlled sentinel effect used by lifecycle scenarios."""

from __future__ import annotations

import asyncio

from agentbarrier.errors import AmbiguousEffectError
from agentbarrier.journal import EffectJournal
from agentbarrier.models import ActionRequest, EffectPhase


class EffectProbe:
    """Records a real external effect and can pause immediately before commit."""

    def __init__(
        self,
        journal: EffectJournal,
        *,
        run_id: str,
        block_before_commit: bool = False,
        raise_after_commit: bool = False,
    ) -> None:
        self.journal = journal
        self.run_id = run_id
        self.block_before_commit = block_before_commit
        self.raise_after_commit = raise_after_commit
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
        self.journal.record(
            run_id=self.run_id,
            action_id=action.action_id,
            tool_name=action.tool_name,
            phase=EffectPhase.COMMITTED,
            arguments=arguments,
        )
        if self.raise_after_commit:
            raise AmbiguousEffectError(action.action_id)
        return f"sentinel effect committed for {action.action_id}"

    async def wait_started(self, timeout_seconds: float) -> None:
        """Wait until at least one invocation crosses the execution boundary."""

        await asyncio.wait_for(self._started.wait(), timeout=timeout_seconds)

    def release(self) -> None:
        """Allow blocked invocations to attempt their commit."""

        self._release.set()
