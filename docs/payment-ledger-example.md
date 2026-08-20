# SQLite payment-ledger example

This example shows where an approval barrier belongs relative to a database transaction. It runs
entirely against temporary local SQLite files: no model, network request, API key, payment
processor, or production service is involved.

> This is control-flow verification code, not production payment code. It omits authentication,
> authorization, currency rules, settlement, disputes, compliance, operational recovery, and many
> other controls required by a real payment system.

## Run it

From the repository checkout:

```bash
uv sync --extra test
uv run python -m examples.run_payment_ledger
```

The first report intentionally fails with `AB002`: the unsafe adapter crosses the effect boundary
while approval is still pending. The runner then shows the corresponding local ledger changing
before approval. The safe adapter passes every capability it declares, and the final demonstration
transfers 2,500 cents only after approval.

The end-to-end lifecycle assertions are also directly runnable:

```bash
uv run pytest tests/test_payment_ledger_example.py -q
```

## The boundary

The proposal carries a stable `ActionRequest.action_id`, used here as the payment operation ID.
The safe path is:

1. Create the payment proposal without touching the ledger.
2. Return the exact proposal for review.
3. Wait for approval bound to that operation ID and its canonical arguments.
4. Enter the effect boundary and begin one SQLite transaction.
5. Debit the customer, credit the merchant, and insert the operation ID and action digest in the
   same transaction.
6. If the response is lost, query the ledger using the original operation ID before considering a
   retry.

The unique `payment_operations.operation_id` constraint is the final replay fence. Reusing an ID
with identical arguments is a no-op; reusing it with different arguments produces explicit
conflict evidence and leaves balances unchanged.

The unsafe adapter reverses the important ordering: it enters the effect boundary before the
approval decision. AgentBarrier observes an actual local transaction, not merely a scheduled
function call, and reports the pre-approval commit.

## Verified behavior

| Lifecycle | Ledger assertion |
| --- | --- |
| Approval | Balances and transaction count remain unchanged while approval is pending. |
| Rejection | No transaction is inserted and no balance changes. |
| Replay | The stable operation ID preserves one transaction and one set of balance changes. |
| Response loss | The run becomes `UNKNOWN`; reconciliation finds the committed ledger row, and replay cannot duplicate it. |
| Cancellation | Cancelling after execution starts but before commit leaves the ledger unchanged. |
| Timeout | A timed-out blocked operation cannot commit after its deadline. |
| Conflict | The same operation ID with different arguments remains unresolved and cannot mutate balances. |

The tests assert the customer and merchant balances and the number of durable payment rows after
each path. Framework state alone is not treated as proof of safety.

## Mapping the pattern to a real tool

In an application adapter, replace only the consequential tool through the application's normal
dependency-injection path. Put the sentinel at the same point where the real implementation would
start its transaction. The production operation should persist its idempotency identity and effect
atomically, and reconciliation should query that authoritative store—not an in-memory task or agent
message history.

A production implementation also needs database isolation appropriate to its workload, durable
authorization and approval records, monetary and currency invariants, an outbox or equivalent for
downstream delivery, observability, recovery queues for `CONFLICT` and `UNAVAILABLE` outcomes, and
careful handling of processor-specific idempotency semantics.
