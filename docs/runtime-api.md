# Runtime API reference

> The runtime API is public in AgentBarrier 0.4.0 and may change before 1.0 with documented release
> notes and migrations. Import public runtime symbols from `agentbarrier.runtime` and runtime
> exceptions from `agentbarrier.errors`.

## Policy

### `RuntimePolicy`

```python
RuntimePolicy(
    version: str,
    rules: tuple[PolicyRule, ...],
    default_effect: PolicyEffect = PolicyEffect.DENY,
)
```

Rules are evaluated in order and the first match wins. If no rule matches, `default_effect` is
used. Rule names must be unique and the policy version participates in every request digest.

- `RuntimePolicy.from_file(path)` loads a strict JSON policy.
- `RuntimePolicy.from_mapping(data)` parses a mapping and rejects unknown keys.
- `RuntimePolicy.evaluate(tool_name, arguments)` returns a `PolicyDecision`.

Use the [runtime-policy-v1 JSON Schema](schemas/runtime-policy-v1.schema.json) to validate policy
files in editors, generators, and deployment pipelines.

### `PolicyRule`

```python
PolicyRule(
    name: str,
    effect: PolicyEffect,
    tool: str = "*",
    conditions: tuple[ArgumentCondition, ...] = (),
    approval_ttl_seconds: float | None = None,
)
```

`tool` is a case-sensitive shell-style glob. Every condition must match. A positive, finite
`approval_ttl_seconds` is valid only for `require_approval` rules.

### `ArgumentCondition`

```python
ArgumentCondition(path: str, operator: ConditionOperator, value: JsonValue = True)
```

`path` traverses nested JSON objects using dot-separated keys. Supported operators are `exists`,
`eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `not_in`, `contains`, `starts_with`, and `ends_with`.
Ordered comparisons require two numbers or two strings; they never coerce types.

`PolicyEffect` values are `allow`, `deny`, and `require_approval`.

## Runtime execution boundary

### `RuntimeBarrier`

```python
RuntimeBarrier(
    *,
    policy: RuntimePolicy,
    store: SQLiteRuntimeStore,
    namespace: str = "default",
)
```

`protect(function, *, tool_name, idempotency_key)` returns a wrapper with the same sync or async
calling shape. `idempotency_key` is either the name of one bound string argument or a selector that
returns a string from the canonical argument mapping.

Arguments and return values must contain only JSON values: `null`, booleans, finite numbers,
strings, lists, and string-keyed objects. The wrapper binds defaults before policy evaluation, so
implicit and explicit default arguments identify the same request.

The wrapper is the complete-mediation boundary. Every route to the consequential function must go
through it; retaining an unwrapped route bypasses AgentBarrier.

Dynamic dispatchers and protocol gateways use the same boundary without synthesizing a Python
signature:

```python
barrier.execute(
    tool_name="payments.refund",
    arguments={"request_id": "refund-1001", "amount": 100},
    idempotency_key="refund-1001",
    operation=call_payment_provider,
)

await barrier.execute_async(
    tool_name="messages.send",
    arguments={"request_id": "message-1001", "to": "person@example.com"},
    idempotency_key="message-1001",
    operation=call_async_provider,
)
```

`execute` and `execute_async` call the supplied zero-argument operation only after the same policy,
approval, exact-binding, and atomic-claim checks used by `protect`. A completed result is returned
from durable storage without invoking the operation again. An exception or cancellation after the
claim produces an `unknown` action and is never retried automatically.

## SQLite runtime store

### `SQLiteRuntimeStore`

```python
SQLiteRuntimeStore(
    path: str | Path,
    *,
    execution_lease_seconds: float = 300,
)
```

Opening a store creates a new database or atomically migrates a supported older schema. The store
uses WAL mode, full synchronous writes, foreign keys, a busy timeout, and immediate transactions
for lifecycle changes.

Application code normally uses the store through `RuntimeBarrier`. Operational and integration
code can use these methods:

- `get_action(action_id)` and `list_actions(status=None)` return immutable snapshots.
- `decide(action_id, decision, decided_by=..., reason=None)` approves or rejects a pending action.
- `reconcile(action_id, outcome, resolved_by=..., reason=..., result=None)` resolves an `unknown`
  outcome using evidence from the real downstream system.
- `receipts(action_id=None)` returns ordered audit receipts.
- `verify_receipt_chain()` verifies the global SHA-256 link and payload digest of every receipt.
- `backup(destination)` writes a consistent, integrity-checked, user-readable-only backup and
  refuses to replace an existing file.
- `schema_version` returns the migrated schema version.

`submit`, `claim`, `complete`, and `mark_unknown` are public for custom effect-boundary adapters.
Callers must preserve their exact request digest and must never call the consequential effect until
`claim` returns `ClaimOutcome.EXECUTE`.

## Lifecycle

| Status | Meaning | Executable? |
| --- | --- | --- |
| `pending` | Exact request is waiting for a reviewer. | No |
| `approved` | Policy or a reviewer authorized this exact request. | Can be claimed once |
| `rejected` | Reviewer rejected the request. | No |
| `denied` | Policy denied the request. | No |
| `expired` | Approval window elapsed before a claim. | No |
| `executing` | One worker owns a time-bounded execution claim. | No second claim |
| `succeeded` | JSON result is durable and replayable. | Replays stored result |
| `unknown` | Execution started but the external outcome is not proven. | No automatic retry |

An expired execution lease moves `executing` to `unknown`; it does not make the action retryable.
`RuntimeReconciliation.COMMITTED` stores a proven result for replay.
`RuntimeReconciliation.NOT_COMMITTED` returns a policy-allowed action to `approved` or an
approval-gated action to `pending` with a fresh approval window.

## Runtime exceptions

All runtime exceptions derive from `RuntimeBarrierError` in `agentbarrier.errors`.

- `ApprovalRequired`, `PolicyDenied`, `ApprovalRejected`, `ApprovalExpired`, `ActionInProgress`,
  and `ActionOutcomeUnknown` include the current `RuntimeAction` as `.action`.
- `ActionBindingError` means an idempotency key or execution digest was reused with different bound
  data.
- `FrameworkControlSignalError` means a claimed framework tool emitted a retry, failure, approval,
  or deferral signal that could otherwise trigger unsafe model-visible recovery. The durable action
  remains `unknown` and requires reconciliation.
- `InvalidActionState` means the requested lifecycle transition is not legal.

Treat `ActionOutcomeUnknown` as an operations event. Do not convert it into an automatic retry.

## CLI equivalents

The [runtime guide](runtime.md) covers the approval, reconciliation, audit, migration, status, and
backup commands. CLI configuration and state errors return exit status 2; a broken receipt chain
returns exit status 1.
