# Roadmap

AgentBarrier's roadmap is organized around one outcome: make control-plane safety regressions easy
to reproduce locally, enforce in CI, and prevent at the production effect boundary.

## 0.2.0 — broader framework coverage

- [x] Add a PydanticAI adapter and credential-free integration tests.
- [x] Add a Google Agent Development Kit adapter and credential-free integration tests.
- [x] Add an AutoGen adapter and credential-free integration tests.
- [x] Publish a reproducible visual demonstration of a real pre-approval side effect.
- [x] Make console failures explain the expected boundary, observed effect, and repair direction.
- [x] Expand the compatibility matrix with tested package and Python versions.
- [x] Add copy-ready CI examples for application adapters.

## 0.3.0 — explicit policy and reproducible evidence

Track the complete release in the [`0.3.0` milestone](https://github.com/binaydhakal/agentbarrier/milestone/1).

- [x] Add reusable [per-action and strict run-wide approval-barrier profiles](https://github.com/binaydhakal/agentbarrier/issues/5).
- [x] Generate [machine-readable compatibility evidence in CI](https://github.com/binaydhakal/agentbarrier/issues/6).
- [x] Improve [reconciliation coverage for ambiguous post-commit outcomes](https://github.com/binaydhakal/agentbarrier/issues/7).
- [x] Add a realistic, credential-free [SQLite payment-ledger example](https://github.com/binaydhakal/agentbarrier/issues/8).
- [x] Complete a deterministic [CrewAI adapter evaluation](https://github.com/binaydhakal/agentbarrier/issues/9).
- [x] Pass the complete [`0.3.0` release checklist](https://github.com/binaydhakal/agentbarrier/issues/10).

## 0.4.0 — runtime enforcement

Track the complete release in the [`0.4.0` milestone](https://github.com/binaydhakal/agentbarrier/milestone/2).

- [x] Add a deterministic runtime policy engine with allow, deny, and approval decisions.
- [x] Protect synchronous and asynchronous Python tool functions at their effect boundary.
- [x] Persist approval requests and execution state in a concurrency-safe SQLite store.
- [x] Bind approvals and idempotency keys to the exact reviewed tool arguments and policy version.
- [x] Add CLI approval, rejection, inspection, reconciliation, and audit-receipt workflows.
- [x] Document and test fail-closed recovery for interrupted or ambiguous executions.

## 0.5.0 — protocol and service integrations

- [ ] Add an MCP proxy that enforces the same runtime policies for tool discovery and execution.
- [ ] Add an authenticated HTTP approval API and outbound decision webhooks.
- [ ] Connect the runtime layer to supported agent frameworks without duplicating policy logic.
- [ ] Publish framework-neutral conformance evidence for the runtime boundary.

## 0.6.0 — team operations

- [ ] Add a small self-hosted approval dashboard with live pending-action updates.
- [ ] Add a PostgreSQL store with migrations and concurrency parity with SQLite.
- [ ] Add signed Slack approval notifications and decisions.
- [ ] Document secure single-node and team deployment patterns.

## 1.0.0 — stable production contract

- [ ] Stabilize the public runtime API and publish a compatibility and migration policy.
- [ ] Add multi-user roles, scoped authorization, and separation of requester and approver.
- [ ] Add OpenTelemetry traces, metrics, and structured logs without recording secrets by default.
- [ ] Publish production deployment, backup, recovery, upgrade, and threat-model documentation.
- [ ] Complete independent release, install, migration, and end-to-end approval audits.

The detailed scope and release gates live in [the runtime delivery plan](docs/runtime-roadmap.md).

## How work is prioritized

A proposed feature moves up the roadmap when it:

1. catches a consequential side effect that ordinary unit tests commonly miss;
2. can be tested deterministically without a paid model call or production credential;
3. works at the real effect boundary instead of inferring safety from configuration; and
4. produces actionable evidence that a team can run in CI.

Framework requests and control-failure reproductions are welcome through the repository's issue
forms. Security-sensitive reports should follow [SECURITY.md](SECURITY.md) instead of a public
issue.
