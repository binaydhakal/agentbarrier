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
    """Raised when an effect committed but its caller did not receive an outcome."""

    def __init__(self, action_id: str) -> None:
        super().__init__(f"outcome was lost after {action_id!r} committed")
        self.action_id = action_id
