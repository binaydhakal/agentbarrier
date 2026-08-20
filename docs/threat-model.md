# Threat model

AgentBarrier has two boundaries. The test harness finds lifecycle-control failures using harmless
sentinel effects. The runtime layer enforces deterministic policy immediately before a real Python
function crosses a consequential effect boundary.

## Security goals

- A gated effect does not commit before its exact tool and arguments are approved.
- Rejection is terminal for the action and its delegated descendants.
- Cancellation and timeout prevent in-flight work from committing later.
- Stable identities prevent duplicate effects across replay and unknown outcomes.
- A pending approval can enforce a strict run-wide barrier over sibling work.
- Decision receipts bind the action identity, tool, and canonical arguments.
- A runtime approval is usable only for the namespace, tool, arguments, idempotency key, and policy
  version that were reviewed.
- Only one worker can claim an approved runtime action, and completed results are replayed without
  executing the protected function again.
- A missing worker or uncertain post-effect result fails closed as `unknown` and is never retried
  automatically.
- Ordered runtime policy rejects malformed values and unknown fields instead of guessing intent.

## Trusted components

For the test harness, the scenario runner, temporary SQLite journal, and sentinel effect are
trusted. The adapter, framework, application middleware, scheduler, persistence implementation,
and cancellation propagation are under test.

For runtime enforcement, the host operating system, Python process, policy file, application wiring,
SQLite library, database path, reviewer identity supplied to the local CLI, and downstream
idempotency lookup are trusted. The model and model-visible conversation are not trusted to approve,
identify, persist, or reconcile an action.

An application result is meaningful only when the sentinel replaces the production tool at the
same complete-mediation boundary. Replacing a tool earlier tests planning but can miss a later
execution bypass. Replacing it later can allow unsafe work before observation.

Runtime protection is meaningful only when every route to the consequential function uses the
protected wrapper. Code with direct access to the original function, database credentials, payment
client, or runtime database can bypass or tamper with the boundary.

## Runtime controls and residual risks

- Canonical JSON and request digests prevent an approval from being reused with changed bound data.
- SQLite immediate transactions serialize action creation, reviewer decisions, execution claims,
  reconciliation, and receipt insertion across processes.
- Execution leases expose abandoned claims. Expiry produces `unknown`, not a retry. A lease is not a
  distributed lock renewal protocol, so long-running tools must choose an appropriate duration and
  operators must verify that an old worker cannot still commit before reconciling as
  `not_committed`.
- Receipt hashes detect accidental edits and unsophisticated tampering. They are not signatures or
  message authentication codes. A process that can rewrite the database can recompute the entire
  chain.
- The 0.4 CLI is a local single-operator interface. It records the supplied reviewer name but does
  not authenticate that identity or enforce separation of duties. Limit database and shell access
  at the operating-system boundary.
- The database contains tool names, arguments, results, reviewer identities, reasons, and receipts.
  File permissions, encrypted storage, protected backups, retention policy, and secret-free tool
  arguments remain deployment responsibilities.
- Reconciliation trusts external evidence associated with the same business idempotency key. If a
  downstream system cannot prove absence, the action must remain `unknown`.
- Policy order is security-sensitive because the first matching rule wins. Review policy changes,
  use an explicit deny default, assign a new policy version, and validate against the published
  schema before deployment.

## Out of scope

AgentBarrier does not:

- detect prompt injection, malware, secrets, or vulnerable dependencies;
- prove that a model will choose safe actions;
- sandbox malicious adapter or application code;
- authorize a real production operation;
- prove behavior outside the configured late-effect observation window;
- turn a failed guarantee into a vulnerability classification without an application threat model;
- sandbox or authenticate local runtime callers;
- stop code that bypasses the protected function wrapper;
- secure, sign, encrypt, replicate, or retain the SQLite database and its backups;
- authenticate CLI reviewer names or provide multi-user authorization in 0.4;
- guarantee exactly-once behavior in an external system that ignores the business idempotency key;
  or
- prove that an `unknown` effect did not commit without downstream evidence.

The bundled framework probes intentionally use deterministic plans and temporary effects. They
measure minimal framework behavior, not every application configuration.

## Safe operation

Never connect a conformance adapter to a production tool. The supplied `EffectProbe` writes only to
its temporary journal. Run application adapters in an isolated test environment with synthetic
credentials and data.

For runtime use, keep the database and policy outside model-writable locations, restrict them to the
service account, back up before migrations, inspect `database status`, and alert on `unknown`,
invalid receipt chains, rejected binding reuse, and repeated policy denials. Protect downstream
systems with their own authorization and idempotency enforcement as a second boundary.
