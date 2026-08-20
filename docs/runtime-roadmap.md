# Runtime delivery plan

AgentBarrier is a runtime policy gateway and approval control plane with a deterministic safety
test suite. Its primary production job is to mediate real agent actions; the test suite makes every
runtime guarantee reproducible in CI.

## Boundary and invariants

The runtime boundary sits immediately in front of a consequential Python function or MCP tool.
AgentBarrier does not ask a model whether an action is safe. It evaluates deterministic policy and
fails closed when state, identity, or outcome is uncertain.

Every runtime implementation must preserve these invariants:

1. An approval is bound to the namespace, tool, canonical arguments, idempotency key, and policy
   version that were reviewed.
2. Reusing an idempotency key with different bound data is rejected before execution.
3. Only one worker can claim an approved action for execution.
4. A completed action is replayed from its recorded result and is not executed twice.
5. A worker crash or exception after execution starts produces an unknown outcome and is never
   retried automatically.
6. Policy decisions and state transitions produce ordered, integrity-checkable audit receipts.
7. Approval and execution state is stored outside model-visible context.
8. Deny, expiry, storage failure, malformed policy, and malformed arguments fail closed.

## 0.4.0 — runtime enforcement

Deliverables:

- ordered allow, deny, and require-approval rules with exact argument conditions;
- wrappers for synchronous and asynchronous JSON-compatible Python tool functions;
- a durable SQLite approval and execution store using atomic state transitions;
- exact request digests, idempotent result replay, expiry, and unknown-outcome handling;
- CLI commands to list, inspect, approve, reject, reconcile, and audit actions;
- tamper-evident receipt-chain verification;
- a credential-free refund example and complete API documentation.

Release gates:

- policy, store, concurrency, crash, sync, async, CLI, and example tests pass on Python 3.10–3.13;
- branch coverage remains at least 90 percent;
- lint, formatting, strict typing, build, and package metadata checks pass;
- a clean environment installs the candidate and completes a real pending → approved → executed →
  replayed flow without executing the protected function twice.

The package CI runs this last gate from the built wheel with `tools/audit_runtime_wheel.py`; its
effect ledger must contain exactly one row and its receipt sequence must include the replay.

## 0.5.0 — protocol and service integrations

Deliverables:

- a transparent MCP stdio and Streamable HTTP proxy using the 0.4 policy and store contracts;
- authenticated HTTP endpoints for action inspection and approval decisions;
- signed, retried outbound webhooks for action and decision events;
- runtime adapters for the supported agent frameworks where the real tool boundary is available;
- protocol-level conformance fixtures that do not require model credentials.

The development branch now has the transport-neutral runtime boundary, MCP tool gateway, scoped
approval API, and durable signed webhook worker. Raw transport conformance, framework runtime
adapters, and clean-wheel audits remain release gates.

Release gates:

- malformed JSON-RPC, cancellation, reconnect, duplicate request, and upstream failure tests pass;
- API authorization, replay protection, webhook signatures, retry limits, and secret-redaction tests
  pass;
- proxy and direct-Python execution produce equivalent policy decisions and receipts.

## 0.6.0 — team operations

Deliverables:

- a self-hosted approval dashboard that never exposes database or signing credentials to browsers;
- PostgreSQL storage with schema migrations and behavioral parity with SQLite;
- Slack notifications and interactive decisions with request signing and identity binding;
- an emergency pause switch and per-tool or per-principal action limits to reduce agent blast
  radius without requiring manual review of every low-risk call;
- container and local deployment examples with secure defaults.

Release gates:

- accessibility, authorization, cross-site request forgery, content-security-policy, and secret
  handling checks pass for the dashboard;
- SQLite and PostgreSQL pass the same store contract and concurrency suite;
- forged, replayed, expired, and unauthorized Slack requests are rejected.
- limits remain atomic under concurrent workers and the emergency pause fails closed.

## 1.0.0 — stable production contract

Deliverables:

- a documented stable API with deprecation and migration guarantees;
- organizations, users, service identities, roles, scoped permissions, and separation of duties;
- OpenTelemetry spans, metrics, and structured logs with privacy-safe defaults;
- production deployment, backup, restore, migration, incident, and threat-model guides;
- an upgrade path from every public runtime release.

Release gates:

- every documented authorization rule is enforced at the store transaction boundary;
- observability exports never contain configured sensitive argument paths;
- backup, restore, rolling migration, and downgrade-failure exercises are reproducible;
- all supported installation and upgrade paths pass in clean environments;
- the public 1.0 artifacts match the audited source tag and recorded digests.

Publishing a version remains a separate release gate. Packages and tags are published only after
the implementation, documentation, compatibility matrix, and clean-install audit for that version
are complete.
