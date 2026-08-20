# Adapter contract

An AgentBarrier adapter is the boundary between the standard scenario suite and an agent
framework or application. The suite owns the action identifiers, sentinel effect, timing, and
assertions. The adapter owns only lifecycle orchestration.

## Required behavior

`AgentAdapter.begin()` must:

1. preserve the supplied `run_id` and `ActionRequest.action_id` values;
2. preserve and enforce `parent_action_id` when delegation is declared;
3. arrange for the supplied `EffectProbe` to be called instead of a real side-effecting tool;
4. return promptly with a `RunHandle` while the run continues asynchronously;
5. apply `timeout_seconds` to the complete effect lifecycle when timeout is declared; and
6. avoid catching cancellation in a way that permits the effect to commit later.

`RunHandle` must:

- surface every pending action from `wait_for_pending()`;
- bind `approve()` and `reject()` to the exact `action_id`;
- execute replacement arguments when `approve(..., replacement=...)` is supported;
- make `cancel()` terminal for the logical run;
- normalize terminal state through `wait()`;
- return bounded, identity-bound evidence from `reconcile()` when outcome reconciliation is
  declared;
- preserve action identity through `replay()`;
- return durable, ordered decision evidence from `audit_receipts()` when declared; and
- release background tasks from `close()`.

Only declare capabilities that the implementation can exercise. Missing capabilities are visible
as skips and become failures when callers select `strict_skips`.

## Outcome reconciliation

`outcome_reconciliation` is additive to the existing `outcome_ambiguity` contract. The existing
capability still verifies that a lost post-commit response becomes `UNKNOWN` and that stable replay
identity prevents duplication. Adapters declaring the stronger reconciliation capability must
also implement `RunHandle.reconcile(action_id, timeout_seconds)`.

The lookup must use the exact stable action or idempotency identity and return `COMMITTED`,
`NOT_COMMITTED`, `CONFLICT`, or `UNAVAILABLE` evidence. Only `NOT_COMMITTED` authorizes an effect
attempt; `COMMITTED` is terminal, while conflicting or unavailable evidence must preserve the
unknown outcome for recovery. Every lookup is bounded and emits identity-bound start and terminal
audit receipts.

## Approval-barrier profiles

The `parallel_barrier` scenario supports two explicit expectations:

- `run-wide` requires every sibling effect to remain uncommitted while any action is pending
  approval. This is the default and strictest profile.
- `per-action` allows ungated siblings to proceed but still requires the gated action to remain
  uncommitted until its own approval.

Profiles are runner expectations, not adapter configuration. An adapter with a stricter run-wide
hold passes both profiles. Application tests should choose the weakest contract the application is
actually prepared to enforce, and retain the profile field from report evidence.

## Complete mediation

The sentinel must be invoked at the same boundary where the production tool would create its
external effect. Invoking it earlier only tests scheduling; invoking it later can hide unsafe work.
An adapter should never call a production API from the conformance suite.

## Correct example

`agentbarrier.adapters.reference.ReferenceAdapter` is the executable contract. It uses a global
approval hold, stable action identities, cancellation propagation, timeout cancellation, and
effect-boundary replay deduplication.

## Audit receipts

When `audit_receipts` is declared, an application should produce receipts through its real audit
path and return them from `RunHandle.audit_receipts()`. Receipts must bind the canonical tool and
arguments with `agentbarrier.action_digest()`.

## Observation window

Late-effect checks use the configurable `settle_seconds` window after a run claims to be terminal.
The bundled scenarios control their sentinel timing, but an arbitrary external worker can outlive
any finite test. Choose a larger window for application adapters whose queues or worker shutdown
paths are slower than the local defaults.

## Application adapter

An application adapter should build the application through its normal dependency-injection path
while replacing only consequential tools with the supplied sentinel. This tests application
middleware and framework behavior together. Do not build a simplified agent that bypasses the
application's real approval or persistence configuration.
