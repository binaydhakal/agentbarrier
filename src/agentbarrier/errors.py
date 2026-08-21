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


class FrameworkControlSignalError(RuntimeBarrierError):
    """Raised when a claimed tool emits a framework signal that could trigger unsafe recovery."""

    def __init__(self, framework: str, signal: str) -> None:
        super().__init__(
            f"{framework} control signal {signal!r} was suppressed after the runtime claim"
        )
        self.framework = framework
        self.signal = signal


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


class EmergencyPauseActive(RuntimeActionError):
    """Raised when an operator pause blocks an action at the execution boundary."""

    def __init__(self, action: RuntimeAction, *, scope: str, reason: str) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} is blocked by emergency pause {scope!r}: {reason}",
        )
        self.scope = scope
        self.reason = reason


class ActionLimitExceeded(RuntimeActionError):
    """Raised when an action would exceed an atomic execution limit."""

    def __init__(
        self,
        action: RuntimeAction,
        *,
        limit_id: str,
        resource: str,
        used: int,
        requested: int,
        maximum: int,
    ) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} would exceed {resource} limit {limit_id!r} "
            f"({used} + {requested} > {maximum})",
        )
        self.limit_id = limit_id
        self.resource = resource
        self.used = used
        self.requested = requested
        self.maximum = maximum


class ActionLimitValueError(RuntimeActionError):
    """Raised when a configured value budget cannot safely price an action."""

    def __init__(self, action: RuntimeAction, *, limit_id: str, value_argument: str) -> None:
        super().__init__(
            action,
            f"action {action.action_id!r} has no non-negative integer at "
            f"{value_argument!r} required by limit {limit_id!r}",
        )
        self.limit_id = limit_id
        self.value_argument = value_argument
