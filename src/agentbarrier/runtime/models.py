"""Immutable runtime policy, action, and receipt value objects."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from agentbarrier.models import JsonValue


class PolicyEffect(str, Enum):
    """Deterministic outcome of evaluating one runtime policy."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RuntimeStatus(str, Enum):
    """Durable lifecycle state of a protected runtime action."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class RuntimeEvent(str, Enum):
    """Audit events emitted by the runtime enforcement boundary."""

    POLICY_ALLOWED = "policy_allowed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_UNKNOWN = "execution_unknown"
    EXECUTION_ABANDONED = "execution_abandoned"
    RECONCILIATION_COMMITTED = "reconciliation_committed"
    RECONCILIATION_NOT_COMMITTED = "reconciliation_not_committed"
    RESULT_REPLAYED = "result_replayed"
    EMERGENCY_PAUSE_BLOCKED = "emergency_pause_blocked"
    LIMIT_BLOCKED = "limit_blocked"


class RuntimeControlEvent(str, Enum):
    """Integrity-linked operator changes to runtime safety controls."""

    EMERGENCY_PAUSE_SET = "emergency_pause_set"
    EMERGENCY_PAUSE_CLEARED = "emergency_pause_cleared"
    LIMIT_CONFIGURED = "limit_configured"
    LIMIT_DISABLED = "limit_disabled"


class RuntimeReconciliation(str, Enum):
    """Externally proven resolution of an unknown runtime outcome."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


class ConditionOperator(str, Enum):
    """Supported deterministic comparisons for policy argument conditions."""

    EXISTS = "exists"
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"


class ClaimOutcome(str, Enum):
    """Whether a caller owns execution or receives an earlier result."""

    EXECUTE = "execute"
    REPLAY = "replay"


def canonical_json(value: JsonValue | Mapping[str, JsonValue], *, path: str) -> str:
    """Validate and encode a JSON value using a stable representation."""

    _validate_json(value, path=path)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def detached_json_object(encoded: str) -> Mapping[str, JsonValue]:
    """Decode a canonical object into a detached, read-only top-level mapping."""

    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - callers encode objects
        raise RuntimeError("canonical runtime arguments were not an object")
    return MappingProxyType(cast(dict[str, JsonValue], decoded))


def _validate_json(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            _validate_json(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


@dataclass(frozen=True, slots=True, init=False)
class RuntimeRequest:
    """One exact, idempotent request presented to the runtime boundary."""

    action_id: str
    namespace: str
    tool_name: str
    idempotency_key: str
    policy_version: str
    created_at_ns: int
    _arguments_json: str = field(repr=False)

    def __init__(
        self,
        *,
        action_id: str,
        namespace: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
        policy_version: str,
        created_at_ns: int,
    ) -> None:
        for name, value in (
            ("action_id", action_id),
            ("namespace", namespace),
            ("tool_name", tool_name),
            ("idempotency_key", idempotency_key),
            ("policy_version", policy_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if created_at_ns < 0:
            raise ValueError("created_at_ns must not be negative")
        encoded = canonical_json(dict(arguments), path="arguments")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "created_at_ns", created_at_ns)
        object.__setattr__(self, "_arguments_json", encoded)

    @property
    def arguments(self) -> Mapping[str, JsonValue]:
        """Return a detached snapshot of the exact arguments."""

        return detached_json_object(self._arguments_json)

    @property
    def request_digest(self) -> str:
        """Bind identity, arguments, idempotency, and policy version."""

        payload = json.dumps(
            {
                "namespace": self.namespace,
                "tool_name": self.tool_name,
                "arguments": json.loads(self._arguments_json),
                "idempotency_key": self.idempotency_key,
                "policy_version": self.policy_version,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A policy result attached to a durable action."""

    effect: PolicyEffect
    rule_name: str
    policy_version: str
    approval_ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.rule_name.strip():
            raise ValueError("policy decision rule_name must not be empty")
        if not self.policy_version.strip():
            raise ValueError("policy decision version must not be empty")
        if self.approval_ttl_seconds is not None:
            if (
                not math.isfinite(self.approval_ttl_seconds)
                or self.approval_ttl_seconds <= 0
                or int(self.approval_ttl_seconds * 1_000_000_000) < 1
            ):
                raise ValueError("approval_ttl_seconds must be finite and at least one nanosecond")
            if self.effect is not PolicyEffect.REQUIRE_APPROVAL:
                raise ValueError(
                    "approval_ttl_seconds is valid only for require_approval decisions"
                )


@dataclass(frozen=True, slots=True)
class RuntimeAction:
    """Immutable snapshot of a stored runtime action."""

    action_id: str
    namespace: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str
    request_digest: str
    policy_version: str
    policy_rule: str
    policy_effect: PolicyEffect
    status: RuntimeStatus
    created_at_ns: int
    updated_at_ns: int
    expires_at_ns: int | None = None
    approval_ttl_ns: int | None = None
    execution_lease_expires_at_ns: int | None = None
    result: JsonValue = None
    result_available: bool = False
    error: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    """One integrity-linked runtime state transition."""

    sequence: int
    action_id: str
    event: RuntimeEvent
    timestamp_ns: int
    request_digest: str
    actor: str | None
    detail: str | None
    previous_hash: str | None
    receipt_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """Atomic result of trying to claim an approved action."""

    outcome: ClaimOutcome
    action: RuntimeAction
    result: JsonValue = None


@dataclass(frozen=True, slots=True)
class RuntimePause:
    """One active emergency pause scope."""

    namespace: str | None
    tool_name: str | None
    paused_at_ns: int
    paused_by: str
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeLimit:
    """One fixed-window execution limit enforced at the claim boundary."""

    limit_id: str
    namespace: str | None
    tool_name: str | None
    window_ns: int
    max_actions: int | None
    value_argument: str | None
    max_value: int | None
    enabled: bool
    updated_at_ns: int
    updated_by: str
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeLimitUsage:
    """Current fixed-window usage for one configured execution limit."""

    limit_id: str
    window_started_at_ns: int
    actions_used: int
    value_used: int


@dataclass(frozen=True, slots=True)
class RuntimeControlReceipt:
    """One integrity-linked operator change to runtime safety controls."""

    sequence: int
    event: RuntimeControlEvent
    timestamp_ns: int
    actor: str
    scope: str
    detail: str
    previous_hash: str | None
    receipt_hash: str
