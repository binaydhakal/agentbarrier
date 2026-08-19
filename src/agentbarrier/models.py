"""Value objects shared by adapters, scenarios, and reporters."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import cast

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class Capability(str, Enum):
    """Lifecycle guarantees an adapter can exercise."""

    APPROVAL = "approval"
    REJECTION = "rejection"
    ARGUMENT_BINDING = "argument_binding"
    REPLAY = "replay"
    CANCELLATION = "cancellation"
    TIMEOUT = "timeout"
    PARALLEL_BARRIER = "parallel_barrier"
    OUTCOME_AMBIGUITY = "outcome_ambiguity"
    AUDIT_RECEIPTS = "audit_receipts"
    DELEGATION = "delegation"


class Decision(str, Enum):
    """A decision applied to a pending action."""

    APPROVE = "approve"
    REJECT = "reject"


class EffectPhase(str, Enum):
    """Observable phases of a sentinel effect."""

    STARTED = "started"
    COMMITTED = "committed"
    ABORTED = "aborted"


class AuditEvent(str, Enum):
    """Control-plane transitions recorded by an adapter under test."""

    RUN_STARTED = "run_started"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUN_COMPLETED = "run_completed"
    RUN_CANCELLED = "run_cancelled"
    RUN_TIMED_OUT = "run_timed_out"
    RUN_UNKNOWN = "run_unknown"
    RUN_FAILED = "run_failed"


class RunStatus(str, Enum):
    """Terminal state reported by a run handle."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ScenarioStatus(str, Enum):
    """Outcome of one guarantee scenario."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True, slots=True, init=False)
class ActionRequest:
    """A deterministic side-effecting action proposed by a test run."""

    action_id: str
    tool_name: str
    requires_approval: bool
    parent_action_id: str | None
    _arguments_json: str = field(repr=False)

    def __init__(
        self,
        action_id: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
        requires_approval: bool = True,
        parent_action_id: str | None = None,
    ) -> None:
        if not action_id.strip():
            raise ValueError("action_id must not be empty")
        if not tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if parent_action_id is not None and not parent_action_id.strip():
            raise ValueError("parent_action_id must not be empty")
        canonical = dict(arguments)
        _validate_json(canonical, path="arguments")
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "requires_approval", requires_approval)
        object.__setattr__(self, "parent_action_id", parent_action_id)
        object.__setattr__(self, "_arguments_json", encoded)

    @property
    def arguments(self) -> Mapping[str, JsonValue]:
        """Return a deeply detached view of the canonical approved arguments."""

        decoded = json.loads(self._arguments_json)
        if not isinstance(decoded, dict):  # pragma: no cover - constructor guarantees an object
            raise RuntimeError("canonical action arguments were not an object")
        return MappingProxyType(cast(dict[str, JsonValue], decoded))

    def with_arguments(self, arguments: Mapping[str, JsonValue]) -> ActionRequest:
        """Return an otherwise identical request with replacement arguments."""

        return ActionRequest(
            action_id=self.action_id,
            tool_name=self.tool_name,
            arguments=arguments,
            requires_approval=self.requires_approval,
            parent_action_id=self.parent_action_id,
        )


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
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            _validate_json(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class EffectEvent:
    """An effect observation persisted outside the adapter under test."""

    sequence: int
    run_id: str
    action_id: str
    tool_name: str
    phase: EffectPhase
    arguments: Mapping[str, JsonValue]
    timestamp_ns: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    """One adapter-supplied control decision or terminal transition."""

    sequence: int
    run_id: str
    event: AuditEvent
    timestamp_ns: int
    action_id: str | None = None
    action_digest: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Terminal result returned by a run handle."""

    status: RunStatus
    detail: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Finding:
    """A precise failed guarantee."""

    code: str
    title: str
    expected: str
    observed: str
    remediation: str


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Result and evidence for one scenario."""

    scenario_id: str
    name: str
    adapter: str
    status: ScenarioStatus
    duration_seconds: float
    events: tuple[EffectEvent, ...] = ()
    receipts: tuple[AuditReceipt, ...] = ()
    finding: Finding | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """Aggregate result for a complete adapter verification run."""

    adapter: str
    results: tuple[ScenarioResult, ...]
    strict_skips: bool = False

    @property
    def passed_count(self) -> int:
        return sum(result.status is ScenarioStatus.PASSED for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status is ScenarioStatus.FAILED for result in self.results)

    @property
    def error_count(self) -> int:
        return sum(result.status is ScenarioStatus.ERROR for result in self.results)

    @property
    def skipped_count(self) -> int:
        return sum(result.status is ScenarioStatus.SKIPPED for result in self.results)

    @property
    def passed(self) -> bool:
        if self.failed_count or self.error_count:
            return False
        return not (self.strict_skips and self.skipped_count)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def raise_for_failure(self) -> None:
        """Raise a compact assertion error unless every required scenario passed."""

        if self.passed:
            return
        from agentbarrier.errors import SuiteFailure

        problems = [
            f"{result.scenario_id}: {result.status.value}"
            for result in self.results
            if result.status in {ScenarioStatus.FAILED, ScenarioStatus.ERROR}
            or (self.strict_skips and result.status is ScenarioStatus.SKIPPED)
        ]
        raise SuiteFailure("AgentBarrier verification failed: " + ", ".join(problems))


def action_digest(action: ActionRequest) -> str:
    """Bind a receipt to one canonical tool identity and argument object."""

    payload = json.dumps(
        {
            "action_id": action.action_id,
            "tool_name": action.tool_name,
            "arguments": dict(action.arguments),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()
