# Changelog

All notable changes to this project will be documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Transport-neutral sync and async runtime execution APIs for dynamic tool dispatchers, MCP
  gateways, and framework integrations that cannot use a Python decorator.
- MCP v2 gateway foundation using the current 2026-07-28 protocol through the official SDK, with
  stdio and Streamable HTTP runners, exact operation identity, approval holds, duplicate-call
  protection, result replay, reconnect handling, progress forwarding, and fail-closed upstream
  cancellation or failure behavior.
- Optional `mcp` dependency group and `agentbarrier mcp stdio|http` operational commands.
- Authenticated approval HTTP API with SHA-256-configured bearer credentials, exact read/decision/
  audit scopes, identity-bound decisions, idempotent decision replay, stable JSON errors,
  pagination, security headers, request-size limits, and a generated OpenAPI 3.1 document.
- Optional `service` dependency group, safe-loopback `agentbarrier api` runner, and hidden-prompt or
  environment-based `agentbarrier auth hash-token` setup command.
- Durable signed runtime webhooks with strict environment-backed secrets, event filters, automatic
  and configured argument redaction, canonical CloudEvents-shaped bodies, HMAC-SHA256 verification,
  transactional outbox checkpoints, crash-safe claims, bounded retries, dead-letter status, and
  exact operator-triggered recovery commands.

## [0.4.0] - 2026-08-20

### Added

- Ordered runtime policy rules with deterministic allow, deny, and require-approval decisions.
- Sync and async Python function protection with exact argument and policy-version binding.
- Transactional SQLite approval state, atomic execution claims, idempotent result replay, approval
  expiry, and fail-closed unknown outcomes.
- Integrity-linked runtime receipts and CLI workflows for listing, inspecting, approving,
  rejecting, reconciling, and auditing actions.
- Execution leases, fail-closed abandoned-worker recovery, explicit unknown-outcome reconciliation,
  and automatic SQLite schema migration.
- Strict runtime policy JSON Schema, condition validation, public runtime API reference, and runtime
  threat model.
- Runtime database status, migration, and non-overwriting integrity-checked backup commands.
- Credential-free runtime refund example and a versioned delivery plan through 1.0.0.

## [0.3.0] - 2026-08-20

### Added

- Explicit `run-wide` and `per-action` approval-barrier profiles across the Python API, CLI,
  pytest fixture, console output, JSON, JUnit, and SARIF evidence.
- Deterministic, schema-validated compatibility evidence generated from both approval profiles,
  checked against the documentation table, and uploaded for each Python CI job.
- Bounded, identity-bound reconciliation evidence for committed, not-committed, conflicting, and
  unavailable outcomes, including durable audit receipts and guarded retry coverage.
- Credential-free SQLite payment-ledger example with unsafe and safe approval boundaries, atomic
  operation identity, response-loss reconciliation, and final balance assertions.
- Credential-free CrewAI adapter evaluation using its real pre-tool hook for approval, rejection,
  argument binding, and per-action parallel behavior, with isolated compatibility evidence for
  the upstream OpenAI SDK dependency conflict and temporary framework storage that leaves user
  tracing preferences untouched.

## [0.2.1] - 2026-08-19

### Fixed

- Package metadata and `agentbarrier --version` now share one version source so published releases
  cannot report a stale CLI version.

## [0.2.0] - 2026-08-19

### Added

- Reproducible terminal recording that demonstrates an unsafe pre-approval effect and the safe
  reference result for the same guarantee.
- Optional ANSI color for terminal reports through `--color auto|always|never`.
- Credential-free PydanticAI adapter covering approval, rejection, argument binding, cancellation,
  timeout, and strict parallel-barrier behavior.
- Credential-free Google Agent Development Kit adapter covering approval, rejection, cancellation,
  timeout, and strict parallel-barrier behavior.
- Credential-free Microsoft AutoGen Core adapter covering approval, rejection, argument binding,
  cancellation, timeout, and strict parallel-barrier behavior.
- Copy-ready GitHub Actions and pytest examples for application adapters.

### Changed

- Console failures now include the finding title, expected behavior, observed evidence, and
  remediation guidance.
- Package metadata now covers agent-safety, human-in-the-loop, tool-calling, guardrail, and
  framework-discovery searches.

## [0.1.0] - 2026-08-19

### Added

- Ten deterministic lifecycle guarantees covering approval, rejection, exact argument binding,
  replay, unknown outcomes, cancellation, timeout, parallel barriers, delegation, and audit
  receipts.
- External SQLite effect journal with durable, ordered evidence.
- Safe reference adapter and public adapter/run-handle contract.
- Credential-free OpenAI Agents Python and LangGraph framework adapters.
- Console, JSON, JUnit, and SARIF reports.
- CLI, pytest fixture, Python 3.10–3.13 support, and strict type checking.

[Unreleased]: https://github.com/binaydhakal/agentbarrier/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/binaydhakal/agentbarrier/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/binaydhakal/agentbarrier/releases/tag/v0.1.0
