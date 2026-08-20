"""Package-specific exceptions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentbarrier.runtime.models import RuntimeAction


class AgentBarrierError(Exception):
    """Base exception for AgentBarrier failures."""


class UnsupportedCapability(AgentBarrierError):
    """Raised when an adapter cannot exercise a requested lifecycle capability."""


class AdapterContractError(AgentBarrierError):
    """Raised when an adapter violates the public lifecycle contract."""


class SuiteFailure(AssertionError):
    """Raised when a suite contains failures, errors, or disallowed skips."""


class AmbiguousEffectError(AgentBarrierError):
    """Raised when the caller cannot determine whether an effect committed."""

    def __init__(self, action_id: str, *, commit_observed: bool = True) -> None:
        detail = (
            f"outcome was lost after {action_id!r} committed"
            if commit_observed
            else f"outcome was lost before {action_id!r} commit could be confirmed"
        )
        super().__init__(detail)
        self.action_id = action_id
        self.commit_observed = commit_observed


class RuntimeBarrierError(AgentBarrierError):
    """Base exception for runtime policy and execution failures."""


class RuntimeActionError(RuntimeBarrierError):
    """Base exception carrying the durable runtime action that failed closed."""

    def __init__(self, action: RuntimeAction, message: str) -> None:
        super().__init__(message)
        self.action = action


class ApprovalRequired(RuntimeActionError):
    """Raised when a protected action is waiting for a reviewer."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} requires approval before {action.tool_name!r} can run",
        )


class PolicyDenied(RuntimeActionError):
    """Raised when runtime policy denies an action."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} was denied by policy rule {action.policy_rule!r}",
        )


class ApprovalRejected(RuntimeActionError):
    """Raised when a reviewer rejected a pending action."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(action, f"action {action.action_id!r} was rejected")


class ApprovalExpired(RuntimeActionError):
    """Raised when an approval request or unused approval expired."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(action, f"approval for action {action.action_id!r} expired")


class ActionInProgress(RuntimeActionError):
    """Raised when another worker already claimed an approved action."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(action, f"action {action.action_id!r} is already executing")


class ActionOutcomeUnknown(RuntimeActionError):
    """Raised when an action started but its final outcome cannot be proven."""

    def __init__(self, action: RuntimeAction) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} has an unknown outcome and will not be retried",
        )


class ActionBindingError(RuntimeBarrierError):
    """Raised when an idempotency key is reused for a different exact request."""


class InvalidActionState(RuntimeBarrierError):
    """Raised when a runtime action cannot perform the requested state transition."""
