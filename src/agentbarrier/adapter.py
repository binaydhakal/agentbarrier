"""Public adapter and run-handle contracts."""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agentbarrier.errors import AdapterContractError, UnsupportedCapability
from agentbarrier.models import ActionRequest, AuditReceipt, Capability, RunOutcome

if TYPE_CHECKING:
    from agentbarrier.probe import EffectProbe


class RunHandle(ABC):
    """Controls and observes one adapter run.

    Implementations must make lifecycle methods idempotent or raise a clear contract error.
    `wait()` must absorb framework cancellation/timeout exceptions and return a `RunOutcome`.
    """

    @abstractmethod
    async def wait_for_pending(self, timeout_seconds: float) -> tuple[ActionRequest, ...]:
        """Wait until approval is requested and return every pending action."""

    @abstractmethod
    async def approve(
        self,
        action_id: str,
        replacement: ActionRequest | None = None,
    ) -> None:
        """Approve one pending action, optionally with reviewed replacement arguments."""

    @abstractmethod
    async def reject(self, action_id: str, reason: str | None = None) -> None:
        """Reject one pending action without executing it."""

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel the run and prevent future effects."""

    @abstractmethod
    async def wait(self, timeout_seconds: float) -> RunOutcome:
        """Wait for a normalized terminal outcome."""

    @abstractmethod
    async def replay(self) -> RunHandle:
        """Replay the same logical run while preserving action identities."""

    @abstractmethod
    async def audit_receipts(self) -> tuple[AuditReceipt, ...]:
        """Return ordered control receipts emitted by this logical run."""

    @abstractmethod
    async def close(self) -> None:
        """Release run resources and stop any remaining background work."""


class AgentAdapter(ABC):
    """Starts deterministic actions through an agent framework or application."""

    name: str
    capabilities: frozenset[Capability]

    @abstractmethod
    async def begin(
        self,
        *,
        run_id: str,
        actions: Sequence[ActionRequest],
        effect: EffectProbe,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        """Start a logical run and return control immediately."""

    def require(self, capability: Capability) -> None:
        """Raise a standard exception when a scenario is unsupported."""

        if capability not in self.capabilities:
            raise UnsupportedCapability(
                f"adapter {self.name!r} does not support {capability.value!r}"
            )


async def wait_for_pending_or_terminal(
    *,
    pending_event: asyncio.Event,
    run_task: asyncio.Task[RunOutcome],
    timeout_seconds: float,
    adapter_name: str,
) -> None:
    """Wait for an approval event while surfacing an early terminal adapter failure."""

    event_task = asyncio.create_task(pending_event.wait())
    done, _ = await asyncio.wait(
        {event_task, run_task},
        timeout=timeout_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if event_task in done and pending_event.is_set():
        return
    if not event_task.done():
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
    if run_task in done:
        outcome = run_task.result()
        detail = f": {outcome.detail}" if outcome.detail else ""
        raise AdapterContractError(
            f"{adapter_name} run terminated before requesting approval "
            f"({outcome.status.value}{detail})"
        )
    raise asyncio.TimeoutError


def validate_actions(actions: Sequence[ActionRequest]) -> tuple[ActionRequest, ...]:
    """Freeze a run's action sequence and reject ambiguous logical identities."""

    normalized = tuple(actions)
    if not normalized:
        raise AdapterContractError("an adapter run requires at least one action")
    seen: set[str] = set()
    duplicates: set[str] = set()
    for action in normalized:
        if action.action_id in seen:
            duplicates.add(action.action_id)
        seen.add(action.action_id)
    if duplicates:
        raise AdapterContractError(
            f"action_id values must be unique within a run: {sorted(duplicates)!r}"
        )
    by_id = {action.action_id: action for action in normalized}
    for action in normalized:
        parent_id = action.parent_action_id
        if parent_id is None:
            continue
        if parent_id == action.action_id:
            raise AdapterContractError("an action cannot delegate to itself")
        if parent_id not in by_id:
            raise AdapterContractError(
                f"parent_action_id {parent_id!r} does not reference an action in this run"
            )
        visited = {action.action_id}
        current = by_id[parent_id]
        while current.parent_action_id is not None:
            if current.action_id in visited:
                raise AdapterContractError("delegation relationships must not contain cycles")
            visited.add(current.action_id)
            next_parent = by_id.get(current.parent_action_id)
            if next_parent is None:
                raise AdapterContractError(
                    f"parent_action_id {current.parent_action_id!r} does not reference an action "
                    "in this run"
                )
            current = next_parent
    return normalized


def validate_timeout(timeout_seconds: float | None) -> float | None:
    """Reject timeout values that cannot define a deterministic deadline."""

    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise AdapterContractError("timeout_seconds must be finite and positive")
    return timeout_seconds
