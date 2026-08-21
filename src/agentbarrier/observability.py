"""Privacy-safe OpenTelemetry and structured logging for runtime action attempts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import metrics, trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from agentbarrier import __version__
from agentbarrier.errors import (
    ActionInProgress,
    ActionLimitExceeded,
    ActionLimitValueError,
    ActionOutcomeUnknown,
    ApprovalExpired,
    ApprovalRejected,
    ApprovalRequired,
    EmergencyPauseActive,
    PolicyDenied,
)
from agentbarrier.runtime.models import RuntimeAction, RuntimeStatus
from agentbarrier.runtime.observation import RuntimeActionObservation

_INSTRUMENTATION_NAME = "agentbarrier.runtime"
_METRIC_DIMENSIONS = frozenset({"organization_id", "namespace", "tool_name"})
_LOGGER = logging.getLogger(_INSTRUMENTATION_NAME)
_LOGGER.addHandler(logging.NullHandler())


class _Counter(Protocol):
    def add(self, amount: int, attributes: dict[str, str]) -> None: ...


class _Histogram(Protocol):
    def record(self, amount: float, attributes: dict[str, str]) -> None: ...


@dataclass(frozen=True, slots=True)
class OpenTelemetryConfig:
    """Privacy and cardinality choices for AgentBarrier telemetry."""

    include_action_id: bool = True
    include_organization_id: bool = False
    include_namespace: bool = True
    include_tool_name: bool = True
    include_policy_version: bool = False
    metric_dimensions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = self.metric_dimensions - _METRIC_DIMENSIONS
        if unknown:
            raise ValueError(
                "unknown AgentBarrier metric dimensions: " + ", ".join(sorted(unknown))
            )


class OpenTelemetryObserver:
    """Emit runtime spans, low-cardinality metrics, and fixed-message structured logs."""

    def __init__(
        self,
        *,
        config: OpenTelemetryConfig | None = None,
        tracer: Tracer | None = None,
        meter: Meter | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config or OpenTelemetryConfig()
        self.tracer = tracer or trace.get_tracer(_INSTRUMENTATION_NAME, __version__)
        self.meter = meter or metrics.get_meter(_INSTRUMENTATION_NAME, __version__)
        self.logger = logger or _LOGGER
        self.clock = clock
        self._attempts: _Counter = self.meter.create_counter(
            "agentbarrier.action.attempts",
            unit="{attempt}",
            description="Protected AgentBarrier action attempts by enforced outcome.",
        )
        self._duration: _Histogram = self.meter.create_histogram(
            "agentbarrier.action.duration",
            unit="s",
            description="Duration of protected AgentBarrier action attempts.",
        )

    @contextmanager
    def observe(
        self,
        *,
        organization_id: str,
        namespace: str,
        tool_name: str,
    ) -> Iterator[RuntimeActionObservation]:
        attributes = self._identity_attributes(
            organization_id=organization_id,
            namespace=namespace,
            tool_name=tool_name,
        )
        span_name = (
            f"agentbarrier.action {tool_name}"
            if self.config.include_tool_name
            else "agentbarrier.action"
        )
        with self.tracer.start_as_current_span(
            span_name,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield _OpenTelemetryActionObservation(
                owner=self,
                span=span,
                organization_id=organization_id,
                namespace=namespace,
                tool_name=tool_name,
                started_at=self.clock(),
            )

    def _identity_attributes(
        self,
        *,
        organization_id: str,
        namespace: str,
        tool_name: str,
    ) -> dict[str, str]:
        attributes: dict[str, str] = {}
        if self.config.include_organization_id:
            attributes["agentbarrier.organization.id"] = organization_id
        if self.config.include_namespace:
            attributes["agentbarrier.namespace"] = namespace
        if self.config.include_tool_name:
            attributes["agentbarrier.tool.name"] = tool_name
        return attributes

    def _metric_attributes(
        self,
        *,
        organization_id: str,
        namespace: str,
        tool_name: str,
    ) -> dict[str, str]:
        values = {
            "organization_id": organization_id,
            "namespace": namespace,
            "tool_name": tool_name,
        }
        return {
            f"agentbarrier.{name.replace('_', '.')}": values[name]
            for name in self.config.metric_dimensions
        }


class _OpenTelemetryActionObservation:
    def __init__(
        self,
        *,
        owner: OpenTelemetryObserver,
        span: Span,
        organization_id: str,
        namespace: str,
        tool_name: str,
        started_at: float,
    ) -> None:
        self.owner = owner
        self.span = span
        self.organization_id = organization_id
        self.namespace = namespace
        self.tool_name = tool_name
        self.started_at = started_at
        self.action: RuntimeAction | None = None
        self.finished = False

    def bind(self, action: RuntimeAction) -> None:
        self.action = action
        attributes: dict[str, str] = {
            "agentbarrier.policy.effect": action.policy_effect.value,
            "agentbarrier.action.status": action.status.value,
        }
        if self.owner.config.include_action_id:
            attributes["agentbarrier.action.id"] = action.action_id
        if self.owner.config.include_policy_version:
            attributes["agentbarrier.policy.version"] = action.policy_version
        self.span.set_attributes(attributes)

    def finish(self, outcome: str, *, action: RuntimeAction) -> None:
        self._emit(outcome, action=action, error_type=None, system_error=False)

    def fail(self, error: BaseException, *, action: RuntimeAction | None) -> None:
        outcome, system_error = (
            ("unknown", True)
            if action is not None and action.status is RuntimeStatus.UNKNOWN
            else _classify_failure(error)
        )
        error_type = type(error).__qualname__ if system_error else None
        self._emit(
            outcome,
            action=action,
            error_type=error_type,
            system_error=system_error,
        )

    def _emit(
        self,
        outcome: str,
        *,
        action: RuntimeAction | None,
        error_type: str | None,
        system_error: bool,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if action is not None:
            self.bind(action)
        self.span.set_attribute("agentbarrier.action.outcome", outcome)
        attributes = self.owner._metric_attributes(
            organization_id=self.organization_id,
            namespace=self.namespace,
            tool_name=self.tool_name,
        )
        attributes["agentbarrier.action.outcome"] = outcome
        if action is not None:
            attributes["agentbarrier.policy.effect"] = action.policy_effect.value
        if error_type is not None:
            attributes["error.type"] = error_type
            self.span.set_attribute("error.type", error_type)
        if system_error:
            self.span.set_status(Status(StatusCode.ERROR))
        duration = max(0.0, self.owner.clock() - self.started_at)
        self.owner._attempts.add(1, attributes)
        self.owner._duration.record(duration, attributes)
        log_attributes = dict(attributes)
        log_attributes.update(
            self.owner._identity_attributes(
                organization_id=self.organization_id,
                namespace=self.namespace,
                tool_name=self.tool_name,
            )
        )
        if action is not None and self.owner.config.include_action_id:
            log_attributes["agentbarrier.action.id"] = action.action_id
        log_attributes["event.name"] = "agentbarrier.action.completed"
        level = logging.ERROR if system_error else logging.INFO
        self.owner.logger.log(
            level,
            "AgentBarrier action attempt completed",
            extra=log_attributes,
        )


def _classify_failure(error: BaseException) -> tuple[str, bool]:
    expected: tuple[tuple[type[BaseException], str], ...] = (
        (ApprovalRequired, "pending"),
        (PolicyDenied, "denied"),
        (ApprovalRejected, "rejected"),
        (ApprovalExpired, "expired"),
        (EmergencyPauseActive, "paused"),
        (ActionLimitExceeded, "limit_blocked"),
        (ActionLimitValueError, "limit_blocked"),
        (ActionInProgress, "in_progress"),
    )
    for error_type, outcome in expected:
        if isinstance(error, error_type):
            return outcome, False
    if isinstance(error, ActionOutcomeUnknown):
        return "unknown", True
    return "error", True


__all__ = ["OpenTelemetryConfig", "OpenTelemetryObserver"]
