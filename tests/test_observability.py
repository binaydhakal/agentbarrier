from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agentbarrier.errors import ApprovalRequired
from agentbarrier.models import Decision, JsonValue
from agentbarrier.observability import OpenTelemetryConfig, OpenTelemetryObserver
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.status: object | None = None

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_attributes(self, attributes: Mapping[str, object]) -> None:
        self.attributes.update(attributes)

    def set_status(self, status: object) -> None:
        self.status = status


class FakeTracer:
    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.spans: list[FakeSpan] = []

    @contextmanager
    def start_as_current_span(self, name: str, **keywords: object) -> Iterator[FakeSpan]:
        self.started.append({"name": name, **keywords})
        span = FakeSpan()
        initial = keywords.get("attributes")
        if isinstance(initial, Mapping):
            span.attributes.update(initial)
        self.spans.append(span)
        yield span


class FakeInstrument:
    def __init__(self) -> None:
        self.measurements: list[tuple[float, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.measurements.append((amount, dict(attributes)))

    def record(self, amount: float, attributes: dict[str, str]) -> None:
        self.measurements.append((amount, dict(attributes)))


class FakeMeter:
    def __init__(self) -> None:
        self.counter = FakeInstrument()
        self.histogram = FakeInstrument()

    def create_counter(self, name: str, **keywords: object) -> FakeInstrument:
        del name, keywords
        return self.counter

    def create_histogram(self, name: str, **keywords: object) -> FakeInstrument:
        del name, keywords
        return self.histogram


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def telemetry() -> tuple[
    OpenTelemetryObserver,
    FakeTracer,
    FakeMeter,
    RecordingHandler,
]:
    tracer = FakeTracer()
    meter = FakeMeter()
    handler = RecordingHandler()
    logger = logging.Logger("agentbarrier-observability-test", level=logging.DEBUG)
    logger.addHandler(handler)
    ticks = iter(float(value) for value in range(20))
    observer = OpenTelemetryObserver(
        tracer=tracer,  # type: ignore[arg-type]
        meter=meter,  # type: ignore[arg-type]
        logger=logger,
        clock=lambda: next(ticks),
    )
    return observer, tracer, meter, handler


def test_observer_emits_pending_success_and_replay_without_sensitive_values(
    tmp_path: Path,
) -> None:
    observer, tracer, meter, handler = telemetry()
    effects: list[str] = []
    policy = RuntimePolicy(
        version="private-policy-v1",
        rules=(
            PolicyRule(
                "review sends",
                PolicyEffect.REQUIRE_APPROVAL,
                tool="messages.send",
            ),
        ),
    )
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=policy,
            store=store,
            namespace="communications",
            organization_id="secret-customer-name",
            requested_by="private-requester@example.com",
            observer=observer,
        )

        def send(request_id: str, token: str) -> dict[str, str]:
            effects.append(request_id)
            return {"delivery": "private-result", "token": token}

        protected = barrier.protect(
            send,
            tool_name="messages.send",
            idempotency_key="request_id",
        )
        with pytest.raises(ApprovalRequired) as pending:
            protected("private-request-id", "private-token")
        with pytest.raises(TypeError, match="Decision"):
            store.decide(pending.value.action.action_id, "approve", decided_by="reviewer")  # type: ignore[arg-type]
        store.decide(pending.value.action.action_id, Decision.APPROVE, decided_by="reviewer")
        assert protected("private-request-id", "private-token")["delivery"] == "private-result"
        assert protected("private-request-id", "private-token")["delivery"] == "private-result"

    assert effects == ["private-request-id"]
    assert [span.attributes["agentbarrier.action.outcome"] for span in tracer.spans] == [
        "pending",
        "succeeded",
        "replayed",
    ]
    assert all(item["record_exception"] is False for item in tracer.started)
    assert all(item["set_status_on_exception"] is False for item in tracer.started)
    assert [
        attributes["agentbarrier.action.outcome"] for _, attributes in meter.counter.measurements
    ] == [
        "pending",
        "succeeded",
        "replayed",
    ]
    assert len(meter.histogram.measurements) == 3
    assert [record.getMessage() for record in handler.records] == [
        "AgentBarrier action attempt completed",
        "AgentBarrier action attempt completed",
        "AgentBarrier action attempt completed",
    ]
    encoded = repr(
        [
            tracer.started,
            [span.attributes for span in tracer.spans],
            meter.counter.measurements,
            [record.__dict__ for record in handler.records],
        ]
    )
    for secret in (
        "private-request-id",
        "private-token",
        "private-result",
        "private-requester@example.com",
        "secret-customer-name",
    ):
        assert secret not in encoded


def test_observer_records_unknown_error_type_without_exception_message(tmp_path: Path) -> None:
    observer, tracer, meter, handler = telemetry()
    policy = RuntimePolicy(
        version="allow-v1",
        rules=(PolicyRule("allow", PolicyEffect.ALLOW, tool="database.write"),),
    )
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(policy=policy, store=store, observer=observer)

        def fail() -> JsonValue:
            raise RuntimeError("private database failure detail")

        with pytest.raises(RuntimeError, match="private database"):
            barrier.execute(
                tool_name="database.write",
                arguments={},
                idempotency_key="write-1",
                operation=fail,
            )

    span = tracer.spans[0]
    assert span.attributes["agentbarrier.action.outcome"] == "unknown"
    assert span.attributes["error.type"] == "RuntimeError"
    assert meter.counter.measurements[0][1]["error.type"] == "RuntimeError"
    assert handler.records[0].levelno == logging.ERROR
    encoded = repr([span.attributes, handler.records[0].__dict__])
    assert "private database failure detail" not in encoded


def test_observer_supports_explicit_high_cardinality_dimensions(tmp_path: Path) -> None:
    tracer = FakeTracer()
    meter = FakeMeter()
    config = OpenTelemetryConfig(
        include_organization_id=True,
        include_policy_version=True,
        metric_dimensions=frozenset({"organization_id", "namespace", "tool_name"}),
    )
    observer = OpenTelemetryObserver(
        config=config,
        tracer=tracer,  # type: ignore[arg-type]
        meter=meter,  # type: ignore[arg-type]
    )
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=RuntimePolicy(
                version="allow-v1",
                rules=(PolicyRule("allow", PolicyEffect.ALLOW, tool="tool"),),
            ),
            store=store,
            organization_id="acme",
            namespace="billing",
            observer=observer,
        )
        assert (
            barrier.execute(
                tool_name="tool",
                arguments={},
                idempotency_key="one",
                operation=lambda: "ok",
            )
            == "ok"
        )

    attributes = meter.counter.measurements[0][1]
    assert attributes["agentbarrier.organization.id"] == "acme"
    assert attributes["agentbarrier.namespace"] == "billing"
    assert attributes["agentbarrier.tool.name"] == "tool"
    assert tracer.spans[0].attributes["agentbarrier.policy.version"] == "allow-v1"

    with pytest.raises(ValueError, match="unknown AgentBarrier metric dimensions"):
        OpenTelemetryConfig(metric_dimensions=frozenset({"action_id"}))


class ExplodingObservation:
    def bind(self, action: object) -> None:
        raise BaseException("telemetry bind failure")

    def finish(self, outcome: str, *, action: object) -> None:
        raise BaseException("telemetry finish failure")

    def fail(self, error: BaseException, *, action: object | None) -> None:
        raise BaseException("telemetry failure failure")


class ExplodingObserver:
    @contextmanager
    def observe(self, **keywords: object) -> Iterator[Any]:
        del keywords
        yield ExplodingObservation()
        raise BaseException("telemetry exit failure")


class EnterExplodingObserver:
    @contextmanager
    def observe(self, **keywords: object) -> Iterator[Any]:
        del keywords
        raise BaseException("telemetry enter failure")
        yield ExplodingObservation()  # pragma: no cover


def test_observer_failures_never_change_protected_action_outcome(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=RuntimePolicy(
                version="allow-v1",
                rules=(PolicyRule("allow", PolicyEffect.ALLOW, tool="tool"),),
            ),
            store=store,
            observer=ExplodingObserver(),  # type: ignore[arg-type]
        )

        result = barrier.execute(
            tool_name="tool",
            arguments={},
            idempotency_key="one",
            operation=lambda: {"status": "committed"},
        )

        def fail_operation() -> JsonValue:
            raise ValueError("original operation failure")

        with pytest.raises(ValueError, match="original operation failure"):
            barrier.execute(
                tool_name="tool",
                arguments={},
                idempotency_key="two",
                operation=fail_operation,
            )

    assert result == {"status": "committed"}


def test_observer_enter_failure_never_prevents_execution(tmp_path: Path) -> None:
    with SQLiteRuntimeStore(tmp_path / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=RuntimePolicy(
                version="allow-v1",
                rules=(PolicyRule("allow", PolicyEffect.ALLOW, tool="tool"),),
            ),
            store=store,
            observer=EnterExplodingObserver(),  # type: ignore[arg-type]
        )
        assert (
            barrier.execute(
                tool_name="tool",
                arguments={},
                idempotency_key="one",
                operation=lambda: "executed",
            )
            == "executed"
        )
