# Runtime enforcement

> The runtime API is under development on `main` for AgentBarrier 0.4.0. The 0.3.x package on PyPI
> contains the deterministic test harness but not this API yet.

AgentBarrier runtime protects a consequential Python function immediately before it crosses the
effect boundary. An ordered policy allows, denies, or pauses the exact call for approval. SQLite
stores the action outside model-visible state and atomically prevents two workers from claiming the
same approval.

Policy files use strict JSON. Validate generated or hand-written policies against the
[`runtime-policy-v1` schema](schemas/runtime-policy-v1.schema.json); unknown fields are rejected by
the runtime as well. See the [runtime API reference](runtime-api.md) for the complete public
contract and lifecycle.

## Protect a function

```python
from agentbarrier.runtime import (
    ArgumentCondition,
    ConditionOperator,
    PolicyEffect,
    PolicyRule,
    RuntimeBarrier,
    RuntimePolicy,
    SQLiteRuntimeStore,
)

policy = RuntimePolicy(
    version="refund-policy-v1",
    rules=(
        PolicyRule(
            "review large refunds",
            PolicyEffect.REQUIRE_APPROVAL,
            tool="payments.refund",
            conditions=(ArgumentCondition("amount", ConditionOperator.GT, 20),),
            approval_ttl_seconds=3600,
        ),
        PolicyRule("allow small refunds", PolicyEffect.ALLOW, tool="payments.refund"),
    ),
)

store = SQLiteRuntimeStore("agentbarrier.db")
barrier = RuntimeBarrier(policy=policy, store=store, namespace="support-agent")


def refund(request_id: str, account_id: str, amount: int) -> dict[str, object]:
    # Call the real payment boundary here.
    return {"request_id": request_id, "status": "refunded"}


safe_refund = barrier.protect(
    refund,
    tool_name="payments.refund",
    idempotency_key="request_id",
)
```

Every argument and result must be JSON-compatible. The idempotency field or selector must return a
non-empty string that uniquely identifies the intended business operation.

Calling an allowed action executes immediately. A reviewed action raises `ApprovalRequired` before
the protected function starts. Reusing its idempotency key with different arguments or a different
policy version raises `ActionBindingError`.

## Review from the CLI

```bash
agentbarrier approvals list --db agentbarrier.db --status pending
agentbarrier approvals show ACTION_ID --db agentbarrier.db --json
agentbarrier approvals approve ACTION_ID \
  --db agentbarrier.db \
  --decided-by alice \
  --reason ticket-123
```

Reject instead with `agentbarrier approvals reject`. Run the application call again after approval.
AgentBarrier atomically claims the stored action, executes it once, records its JSON result, and
returns that stored result on subsequent retries.

## Inspect audit receipts

```bash
agentbarrier audit --db agentbarrier.db
agentbarrier audit --db agentbarrier.db --action-id ACTION_ID --json
```

Receipts form one SHA-256 integrity chain covering policy decisions, human decisions, execution,
and replay. This detects accidental or unauthorized database edits; it is not a substitute for
filesystem permissions, backups, or external signing.

## Manage the runtime database

Opening a database automatically applies supported forward migrations in one transaction. For an
explicit deployment step, status check, and consistent backup, use:

```bash
agentbarrier database status --db agentbarrier.db
agentbarrier database backup --db agentbarrier.db --output backups/agentbarrier.db
agentbarrier database migrate --db agentbarrier.db
```

Back up before upgrading. The backup command refuses to replace an existing path, uses SQLite's
online backup operation, verifies the finished database, and limits its file mode to the current
user. Store the backup outside the application host using encryption and access controls appropriate
for the tool arguments and decisions it contains. Restore by stopping writers, preserving the
current database, copying the verified backup into place, and running `database status` before
restarting the application.

## Unknown outcomes fail closed

Once a worker claims an action, no other worker can execute it. A claim has a five-minute execution
lease by default. If the function raises, the process is cancelled, the JSON result cannot be
stored, or a claimed worker disappears until its lease expires, the action becomes `unknown`.
AgentBarrier will not retry it automatically because the external effect may already have
committed.

First check the real downstream system using the stored tool name, arguments, and idempotency key.
Then record one of the two explicit outcomes:

```bash
# The effect committed: save the result and make future calls replay it.
agentbarrier approvals reconcile ACTION_ID \
  --db agentbarrier.db \
  --outcome committed \
  --resolved-by alice \
  --reason "verified in payment ledger" \
  --result-json '{"status":"refunded"}'

# The effect definitely did not commit: return it to the policy gate.
agentbarrier approvals reconcile ACTION_ID \
  --db agentbarrier.db \
  --outcome not_committed \
  --resolved-by alice \
  --reason "payment provider confirms no matching request"
```

A `not_committed` action that originally required approval returns to `pending` with a fresh
approval window; a policy-allowed action returns to `approved`. Every reconciliation records the
operator identity and reason in the receipt chain. Never choose `not_committed` merely because an
outcome cannot be found quickly—use it only when the downstream system proves the effect did not
happen.

## Run the local refund example

The example writes a real row to a separate SQLite refund ledger. Refunds over 20 pause before the
insert. After approval, running the same command repeatedly still inserts once.

```bash
uv run python -m examples.runtime_refund \
  --db /tmp/agentbarrier-runtime.db \
  --ledger /tmp/agentbarrier-refunds.db \
  --request-id refund-1001 \
  --account-id account-7 \
  --amount 100
```

Use the printed approval command, then repeat the example command.
