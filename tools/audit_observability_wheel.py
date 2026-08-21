"""Audit an installed wheel's real OpenTelemetry trace and metric emission."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentbarrier.observability import OpenTelemetryObserver
from agentbarrier.runtime import (
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def run_audit(directory: Path) -> None:
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    handler = RecordingHandler()
    logger = logging.Logger("agentbarrier-wheel-observability", level=logging.INFO)
    logger.addHandler(handler)
    observer = OpenTelemetryObserver(
        tracer=tracer_provider.get_tracer("agentbarrier-wheel-audit"),
        meter=meter_provider.get_meter("agentbarrier-wheel-audit"),
        logger=logger,
    )
    policy = RuntimePolicy(
        version="observability-wheel-v1",
        rules=(PolicyRule("allow", PolicyEffect.ALLOW, tool="payments.refund"),),
    )
    with SQLiteRuntimeStore(directory / "runtime.db") as store:
        barrier = RuntimeBarrier(
            policy=policy,
            store=store,
            namespace="billing",
            organization_id="private-organization",
            requested_by="private-requester",
            observer=observer,
        )
        result = barrier.execute(
            tool_name="payments.refund",
            arguments={"request_id": "private-id", "credential": "private-secret"},
            idempotency_key="private-id",
            operation=lambda: {"status": "private-result"},
        )
        if result != {"status": "private-result"}:
            raise AssertionError("observed action returned the wrong result")

    spans = span_exporter.get_finished_spans()
    metrics = metric_reader.get_metrics_data()
    if len(spans) != 1 or spans[0].attributes.get("agentbarrier.action.outcome") != "succeeded":
        raise AssertionError("installed wheel did not export the expected action span")
    if metrics is None or "agentbarrier.action.attempts" not in repr(metrics):
        raise AssertionError("installed wheel did not export the expected action metric")
    if len(handler.records) != 1 or handler.records[0].getMessage() != (
        "AgentBarrier action attempt completed"
    ):
        raise AssertionError("installed wheel did not emit the structured lifecycle log")
    exported = repr([spans[0].attributes, metrics, handler.records[0].__dict__])
    for secret in (
        "private-organization",
        "private-requester",
        "private-id",
        "private-secret",
        "private-result",
    ):
        if secret in exported:
            raise AssertionError(f"privacy-safe telemetry leaked {secret!r}")
    tracer_provider.shutdown()
    meter_provider.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agentbarrier-observability-wheel-") as temporary:
        run_audit(Path(temporary))
    print("installed wheel observability audit passed")
