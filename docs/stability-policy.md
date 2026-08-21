# Stability and migration policy

This document defines the compatibility contract for AgentBarrier 1.x. Historical releases before
1.0 were development releases and are outside this compatibility and security-support promise.

## Versioning promise

AgentBarrier follows [Semantic Versioning](https://semver.org/). Starting with 1.0.0:

- patch releases fix defects and security problems without intentionally changing supported public
  behavior;
- minor releases may add optional parameters, symbols, endpoints, fields, enum values, policy
  schema versions, and telemetry, while preserving existing supported use; and
- removal or an incompatible change to the public contract requires a new major release.

A public feature is deprecated in documentation and emits `DeprecationWarning` where Python can
identify its use. It remains available for the rest of the current major release. Security fixes
may reject input that was previously accepted when accepting it would bypass authorization,
complete mediation, tenant isolation, or exact action binding. Those changes are called out in the
security advisory and release notes.

## Supported public surface

The stable Python surface consists only of names listed in `__all__` by these modules:

- `agentbarrier` and `agentbarrier.errors`;
- `agentbarrier.runtime`;
- `agentbarrier.mcp`;
- `agentbarrier.service`;
- `agentbarrier.observability`; and
- the four documented runtime integration modules under `agentbarrier.integrations`.

The integration modules are `openai_agents`, `langgraph`, `pydantic_ai`, and `google_adk`. Their
documented builders remain stable, but upstream framework types and behavior are governed by the
tested dependency ranges in `pyproject.toml`. Modules, attributes, and callables whose names begin
with an underscore are private. Importing a non-exported implementation module does not create a
compatibility guarantee.

Public exception classes in `agentbarrier.errors`, runtime status and event string values, exact
request-binding behavior, fail-closed outcomes, and documented immutable model fields are part of
the contract. New optional fields or enum values may be added in a minor release; consumers must
not assume their current lists are exhaustive.

CI snapshots the exported symbols and the parameter order, kind, and defaults of the main runtime,
service, MCP, webhook, and observability constructors. A deliberate compatible addition requires a
reviewed contract update; an accidental removal or signature change fails the suite.

## Optional dependencies and Python versions

The core package has no runtime dependency. Optional public modules require their named extras:
`mcp`, `service`, `slack`, `postgres`, `postgres-binary`, `observability`, or a framework-specific
extra. Importing an optional module without its extra is not supported.

AgentBarrier 1.x supports CPython 3.10 through 3.13. Dropping a Python minor version during 1.x
requires that version to be end-of-life and is announced in a minor release before support is
removed. Clean-wheel CI exercises every supported Python version.

## HTTP, MCP, CLI, and telemetry

The approval service endpoints under `/v1` are versioned. Within 1.x, existing paths, methods,
required request fields, response meanings, authorization checks, and stable error codes remain
compatible. Clients must ignore unknown response fields and be prepared for new error codes that
fail a request closed. An incompatible wire change uses a new path version.

The MCP gateway follows the negotiated MCP protocol version and preserves the documented
AgentBarrier metadata keys. Supported command names, option names, exit-code meanings, and JSON
output fields are stable during 1.x. Human-readable CLI wording is not a machine interface.

The `agentbarrier.runtime` OpenTelemetry instrumentation scope, published metric names, fixed log
event name, and existing attribute meanings are stable in 1.x. New attributes and outcomes may be
added. Sensitive action arguments, results, requester identities, decision reasons, and exception
messages are not emitted by default.

## Configuration and database compatibility

Published JSON schemas are immutable. A semantic change creates a new schema identifier or config
`version`; it never silently changes an existing version. Runtime policy version strings are
application-controlled and must change whenever reviewed policy semantics change. Authorization
config versions 1 and 2 remain readable throughout 1.x, although version 2 is required for tenant
isolation and separation of duties.

SQLite opens and atomically migrates every database created by a public runtime release. Back up
the database before upgrading. PostgreSQL migration is an explicit operator action and supports the
same forward path. New application code may run only after its required schema migration succeeds.
Older code must not be pointed at a newer schema; rollback means restoring the pre-upgrade backup
or snapshot before starting the older version.

Receipt-chain verification remains available across supported forward migrations. A migration may
add records or columns but must not rewrite existing signed receipt payloads.

## Upgrade procedure

1. Read every intervening release note and pin the target version and artifact digest.
2. Stop decision and execution workers, or drain them until no action is `executing`.
3. Verify both receipt chains and take a database backup or managed snapshot.
4. Test the application, policy, authorization config, and framework integrations against a copy.
5. Run the documented database migration with the migration identity.
6. Deploy the matching application and worker version, then run health and approval lifecycle
   checks before restoring traffic.
7. Keep the pre-upgrade backup until the rollback window closes.

Upgrade and recovery commands are detailed in the deployment and backend guides. Never retry an
`unknown` action as part of an upgrade; reconcile it against the downstream system first.

## Reporting compatibility problems

Open a GitHub issue for an ordinary compatibility regression and include the old and new package
versions plus a minimal reproduction. Report any authorization bypass, secret exposure, tenant
isolation failure, duplicate consequential effect, or receipt-integrity issue privately by
following `SECURITY.md`.
