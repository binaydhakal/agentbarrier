# Threat model

AgentBarrier tests lifecycle controls at a harmless external effect boundary. It is designed to
find accidental or framework-induced side-effect leaks in approval, concurrency, replay,
cancellation, timeout, delegation, and recovery paths.

## Security goals

- A gated effect does not commit before its exact tool and arguments are approved.
- Rejection is terminal for the action and its delegated descendants.
- Cancellation and timeout prevent in-flight work from committing later.
- Stable identities prevent duplicate effects across replay and unknown outcomes.
- A pending approval can enforce a strict run-wide barrier over sibling work.
- Decision receipts bind the action identity, tool, and canonical arguments.

## Trusted components

The scenario runner, temporary SQLite journal, and sentinel effect are part of the test harness.
The adapter, framework, application middleware, scheduler, persistence implementation, and
cancellation propagation are under test.

An application result is meaningful only when the sentinel replaces the production tool at the
same complete-mediation boundary. Replacing a tool earlier tests planning but can miss a later
execution bypass. Replacing it later can allow unsafe work before observation.

## Out of scope

AgentBarrier does not:

- detect prompt injection, malware, secrets, or vulnerable dependencies;
- prove that a model will choose safe actions;
- sandbox malicious adapter or application code;
- authorize a real production operation;
- prove behavior outside the configured late-effect observation window; or
- turn a failed guarantee into a vulnerability classification without an application threat model.

The bundled framework probes intentionally use deterministic plans and temporary effects. They
measure minimal framework behavior, not every application configuration.

## Safe operation

Never connect a conformance adapter to a production tool. The supplied `EffectProbe` writes only to
its temporary journal. Run application adapters in an isolated test environment with synthetic
credentials and data.
