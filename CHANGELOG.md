# Changelog

All notable changes to this project will be documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-08-21

### Added

- Interactive `agentbarrier approvals review` selection with exact action details and
  Approve/Reject/Back/Quit choices, plus an immediate one-command decision menu in the local refund
  demonstration. Non-interactive approvals remain explicit and production reviewers retain the
  authenticated dashboard, API, and Slack paths.

## [1.0.0] - 2026-08-21

AgentBarrier 1.0.0 consolidates the completed 0.4 runtime, 0.5 integration, and 0.6 team-operations
development milestones into the first stable production release. Those milestone versions were
not separately published.

### Added

- Ordered runtime policy rules with deterministic allow, deny, and require-approval decisions.
- Sync and async Python function protection with exact argument and policy-version binding.
- Transactional SQLite approval state, atomic execution claims, idempotent result replay, approval
  expiry, fail-closed unknown outcomes, execution leases, abandoned-worker recovery, and automatic
  schema migration.
- Integrity-linked runtime receipts and CLI workflows for listing, inspecting, approving,
  rejecting, reconciling, auditing, migrating, backing up, and inspecting runtime databases.
- Strict runtime policy JSON Schema, condition validation, public runtime API reference, threat
  model, credential-free refund example, and installed-wheel lifecycle audit.
- A documented 1.x stability and migration policy, explicit public exports for runtime exceptions,
  service webhooks, and MCP runners, plus CI contract snapshots for exported names and critical
  callable signatures.
- A locked, non-root production container, hardened PostgreSQL/approval-API Compose baseline, and
  deployment guide with credential separation, readiness, backup/restore drills, maintenance-window
  upgrades, safe rollback, alerting, and incident response.
- A `postgres-binary` deployment extra so the reference container includes a working isolated
  Psycopg client without requiring system PostgreSQL libraries in the runtime stage.
- Clean-wheel recovery and webhook audits covering schema-v1 migration, safe downgrade refusal,
  backup/restore replay, exact signed delivery bytes, bounded retry, and secret redaction.
- A clean-wheel OpenAI Agents FunctionTool audit covering injected-context exclusion, approval hold,
  exact execution, and durable replay through the real SDK boundary.
- Version 2 multi-user authorization with organizations, exclusive namespace ownership, reusable
  roles, user and service identities, exact approve/reject powers, tenant-filtered API/dashboard/
  Slack views, requester identity binding, and transaction-boundary separation-of-duty checks.
- Organization and requester attribution for Python and MCP runtime barriers, schema version 5
  migrations for SQLite and PostgreSQL, legacy digest compatibility, and organization-scoped
  Slack notifications and decisions.
- Failure-isolated OpenTelemetry runtime spans and low-cardinality metrics plus fixed-message
  structured lifecycle logs, privacy controls, explicit high-cardinality opt-ins, documentation,
  and a real SDK installed-wheel export audit.
- Durable emergency pauses with global, namespace, tool, and combined scopes, enforced atomically
  at the final execution claim and recorded in both action and operator-control receipt chains.
- Fixed-window action and non-negative integer-value limits with atomic cross-process capacity
  reservation, fail-closed value extraction, retained capacity for unknown outcomes, and release
  only after a proven `not_committed` reconciliation.
- `agentbarrier controls` pause, resume, limit configuration, disable, and status commands plus a
  clean-wheel control lifecycle audit.
- Server-rendered approval dashboard with opaque in-memory reviewer sessions, read and decision
  scopes, exact escaped action inspection, identity-bound decisions, CSRF and same-origin
  enforcement, strict form parsing, browser isolation headers, responsive accessible layouts, safe
  loopback defaults, secure-cookie deployment controls, and a clean-wheel approval audit.
- PostgreSQL runtime storage with an explicit migration boundary, secret-safe environment-based
  configuration, dedicated schemas, cross-process advisory locking, SQLite behavioral parity,
  service and CLI integration, real database CI, and an installed-wheel lifecycle audit.
- Signed Slack approval notifications with exact action and posted-message binding, strict
  workspace/app/channel/member authorization, per-member decision permissions, five-minute HMAC
  request verification, durable interaction replay protection, bounded notification retries,
  dead-letter recovery commands, oversized-action fail-closed behavior, and an installed-wheel
  approval audit.

- Transport-neutral sync and async runtime execution APIs for dynamic tool dispatchers, MCP
  gateways, and framework integrations that cannot use a Python decorator.
- MCP v2 gateway foundation using the current 2026-07-28 protocol through the official SDK, with
  stdio and Streamable HTTP runners, exact operation identity, approval holds, duplicate-call
  protection, result replay, reconnect handling, progress forwarding, and fail-closed upstream
  cancellation or failure behavior.
- Scoped bearer authentication for Streamable HTTP clients, mandatory authentication on
  non-loopback listeners, a bounded configurable request size, and secret-manager-friendly
  upstream bearer injection from a named environment variable with redirects disabled and remote
  plaintext upstreams rejected.
- Raw Streamable HTTP authorization, initialization, discovery, execution, replay, malformed
  request, and size-limit conformance fixtures plus an installed-wheel MCP lifecycle audit.
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
- OpenAI Agents Python runtime tool builder with context-aware argument binding, required business
  idempotency, normal SDK schemas and options, durable approval and replay, and fail-closed exception
  and timeout behavior.
- LangGraph runtime tool and fail-closed `ToolNode` builders with injected `ToolRuntime` exclusion,
  business idempotency across model retries, durable approval and replay, and unknown-outcome error
  propagation to the host application.
- PydanticAI runtime tool builder with `RunContext` exclusion, async cancellation safety, business
  idempotency, durable approval and replay, and suppression of framework retry, failure, and
  deferred-call signals after an execution claim.
- Google ADK runtime `FunctionTool` builder with injected `ToolContext` exclusion, async
  cancellation safety, business idempotency, durable approval and replay, and conflicting native
  confirmation or streaming paths disabled.

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

[Unreleased]: https://github.com/binaydhakal/agentbarrier/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/binaydhakal/agentbarrier/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/binaydhakal/agentbarrier/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/binaydhakal/agentbarrier/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/binaydhakal/agentbarrier/releases/tag/v0.1.0
