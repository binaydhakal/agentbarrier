"""Package-specific exceptions."""


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
