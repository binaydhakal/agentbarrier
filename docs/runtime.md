# Runtime enforcement

> The runtime API is under development on `main` for AgentBarrier 0.4.0. The 0.3.x package on PyPI
> contains the deterministic test harness but not this API yet.

AgentBarrier runtime protects a consequential Python function immediately before it crosses the
effect boundary. An ordered policy allows, denies, or pauses the exact call for approval. SQLite
stores the action outside model-visible state and atomically prevents two workers from claiming the
same approval.

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

## Unknown outcomes fail closed

Once a worker claims an action, no other worker can execute it. If the function raises, the process
is cancelled, or the JSON result cannot be stored, the action becomes `unknown`. AgentBarrier will
not retry it automatically because the external effect may already have committed. Reconcile the
business operation using its idempotency key and use a new request only after its outcome is known.

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
