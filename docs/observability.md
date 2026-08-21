# Privacy-safe observability

AgentBarrier can emit OpenTelemetry spans and metrics plus structured Python log records for every
protected runtime attempt. Telemetry is optional and failure-isolated: an unavailable exporter,
broken handler, or observer exception cannot allow, deny, duplicate, or otherwise change an action.

The implementation follows OpenTelemetry's guidance that an instrumented library depend only on
the API while the host application owns SDK and exporter configuration. Traces and metrics are
stable OpenTelemetry Python signals; the Python logging bridge remains the recommended path while
the native logs signal continues to evolve. See the official
[Python instrumentation guide](https://opentelemetry.io/docs/languages/python/instrumentation/),
[instrumentation-library guide](https://opentelemetry.io/docs/languages/python/libraries/), and
[metric cardinality guidance](https://opentelemetry.io/docs/concepts/signals/metrics/).

## Install and enable

Install AgentBarrier's API-only integration and the SDK/exporter selected by the application:

```bash
python -m pip install 'agentbarrier[observability]' \
  opentelemetry-sdk \
  opentelemetry-exporter-otlp-proto-grpc
```

Configure the global OpenTelemetry tracer and meter providers at application startup, then attach
one observer to the runtime boundary:

```python
from agentbarrier.observability import OpenTelemetryObserver
from agentbarrier.runtime import RuntimeBarrier

observer = OpenTelemetryObserver()
barrier = RuntimeBarrier(
    policy=policy,
    store=store,
    namespace="billing",
    organization_id="acme",
    requested_by="refund-agent",
    observer=observer,
)
```

The observer uses the `agentbarrier.runtime` instrumentation scope and the package version. With no
SDK configured, OpenTelemetry's API is a no-op; application behavior is unchanged.

## Signals

Each call through `protect`, `execute`, or `execute_async` produces:

- one `agentbarrier.action TOOL_NAME` span;
- one increment to `agentbarrier.action.attempts`;
- one measurement in `agentbarrier.action.duration`, in seconds; and
- one fixed-message log record from the `agentbarrier.runtime` logger with
  `event.name=agentbarrier.action.completed`.

Outcomes include `pending`, `denied`, `rejected`, `expired`, `paused`, `limit_blocked`,
`in_progress`, `succeeded`, `replayed`, `unknown`, and `error`. Expected policy and approval
controls do not set span error status. Unknown outcomes and unexpected errors set the stable
`error.type` attribute to the exception class name without exporting its message, following
OpenTelemetry's [error guidance](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/).

## Privacy defaults

AgentBarrier never adds tool arguments, results, business idempotency keys, request digests,
requester/reviewer identities, decision reasons, bearer credentials, or exception messages to any
telemetry signal. It also disables automatic exception recording on its spans.

By default:

- spans and structured logs include namespace, tool name, action ID, policy effect, action status,
  and enforced outcome;
- organization ID and policy version are omitted; and
- metrics contain only low-cardinality policy effect, outcome, and error type.

Action IDs are random correlation identifiers, not business idempotency keys. If even opaque action
correlation is disallowed, set `include_action_id=False`.

```python
from agentbarrier.observability import OpenTelemetryConfig, OpenTelemetryObserver

observer = OpenTelemetryObserver(
    config=OpenTelemetryConfig(
        include_action_id=False,
        include_namespace=False,
        include_tool_name=False,
    )
)
```

Organization IDs, namespaces, and tool names can be added as metric dimensions only through an
explicit opt-in. This can create high-cardinality streams and should be paired with SDK views and
cardinality limits:

```python
observer = OpenTelemetryObserver(
    config=OpenTelemetryConfig(
        include_organization_id=True,
        include_policy_version=True,
        metric_dimensions=frozenset({"organization_id", "namespace", "tool_name"}),
    )
)
```

## Structured logging

The log body is always `AgentBarrier action attempt completed`. Machine-readable fields are placed
on the `LogRecord` as extra attributes using the same `agentbarrier.*` names as spans and metrics.
Attach a JSON formatter or an OpenTelemetry logging handler at application startup. AgentBarrier
adds only a silent `NullHandler` to avoid fallback console output; it does not select an exporter or
change the application's root logging level.

Do not enrich these records with raw action payloads at ingress or in a custom processor. Protect
collector credentials, use TLS to the collector, set backend retention, and treat action IDs and
operational metadata as sensitive even though payload data is excluded.

## Recommended alerts

- any `unknown` outcome, because downstream commitment must be reconciled before retry;
- any `error` outcome or rising duration/error rate;
- sustained `pending` volume or approval expiry;
- repeated `limit_blocked` or `paused` outcomes; and
- missing telemetry from a service that is otherwise healthy, without using telemetry absence as
  proof that an action did not execute.

Integrity-linked runtime receipts remain the authoritative action history. Telemetry is an
operational projection and may be sampled, delayed, dropped, or unavailable.
