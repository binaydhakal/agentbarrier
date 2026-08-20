# Framework runtime integrations

Framework runtime integrations put AgentBarrier immediately around a framework tool's original
Python callable. They are different from the compatibility adapters in `agentbarrier.adapters`,
which exercise deterministic sentinel tools to test framework lifecycle guarantees.

The first production integration supports OpenAI Agents Python. LangGraph and the other evaluated
frameworks remain 0.5.0 release work.

## OpenAI Agents Python

> This integration is under development for AgentBarrier 0.5.0 and currently targets
> `openai-agents>=0.22,<1`. Pin an exact pre-1.0 version and run application-specific tests before
> connecting consequential tools.

The official OpenAI Agents documentation defines Python tools with `function_tool`. AgentBarrier's
`runtime_function_tool` returns that same SDK `FunctionTool`, while placing deterministic policy,
durable approval, exact argument binding, execution claims, result replay, and audit receipts around
the original callable. See the official
[agent definition guide](https://developers.openai.com/api/docs/guides/agents/define-agents).

Install the optional integration:

```bash
python -m pip install 'agentbarrier[openai]'
```

Create the runtime policy and store as described in the [runtime guide](runtime.md), then build the
tool:

```python
from typing import Any

from agents import Agent, RunContextWrapper

from agentbarrier.integrations.openai_agents import runtime_function_tool
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, SQLiteRuntimeStore


async def refund_payment(
    context: RunContextWrapper[dict[str, Any]],
    request_id: str,
    amount: int,
) -> dict[str, object]:
    payment_client = context.context["payment_client"]
    return await payment_client.refund(request_id=request_id, amount=amount)


store = SQLiteRuntimeStore("agentbarrier.db")
barrier = RuntimeBarrier(
    policy=RuntimePolicy.from_file("policy.json"),
    store=store,
    namespace="support-agent",
)
refund_tool = runtime_function_tool(
    refund_payment,
    barrier=barrier,
    idempotency_key="request_id",
    name_override="payments_refund",
    description_override="Refund one exact payment request.",
)
agent = Agent(
    name="Support agent",
    instructions="Help customers and request refunds when needed.",
    tools=[refund_tool],
)
```

Keep the store open for the complete lifetime of every agent run that can call the tool. In a web
service, create it during application startup and close it during shutdown rather than constructing
it inside the tool.

The first call matching `require_approval` raises `ApprovalRequired` before the original function
runs. Record the action ID, let a reviewer decide through the authenticated
[approval API](approval-api.md), then resume or retry the agent operation with the same stable
`request_id` and exact arguments. AgentBarrier claims the approved action once. Later framework
retries return its stored result without calling the payment client again.

### Operation identity

`idempotency_key` accepts either the name of a top-level string argument or a callable that derives
a string from the exact policy arguments:

```python
idempotency_key = lambda arguments: str(arguments["request_id"])
```

Use an application-controlled business identifier that remains stable across agent runs, model
retries, process restarts, and approval resumption. Do not use the OpenAI SDK `tool_call_id`; it is a
protocol invocation identifier and is not durable business identity.

The integration excludes the SDK-injected `RunContextWrapper` from policy arguments. Ordinary
function defaults are applied and included, so the approval remains bound to the exact call the
original function receives.

### Fail-closed SDK settings

The integration owns approval at the AgentBarrier boundary and rejects conflicting SDK settings:

- `needs_approval` must remain false. Enabling it would create two independent approval authorities.
- `failure_error_function` must remain `None`. Otherwise the SDK could convert an
  `ApprovalRequired`, denial, or uncertain effect into ordinary model-visible tool output.
- `timeout_behavior` is fixed to `raise_exception`. If an SDK timeout cancels a claimed operation,
  AgentBarrier records `unknown`, raises the timeout to the host, and never retries automatically.

Input guardrails, output guardrails, tool enablement, strict schema behavior, timeouts, deferred
loading, allowed callers, and output schemas remain normal `function_tool` options. Input
guardrails run before the callable. Output guardrails run after a successful AgentBarrier result.

The protected function's return value must be JSON-compatible because AgentBarrier persists the
exact result for duplicate suppression and replay. For non-JSON SDK output objects, return a stable
JSON representation from the consequential function and construct display-only objects outside the
effect boundary. Generator, async-generator, and opaque callable-object tools are rejected because
their work can occur later during iteration or indirection, outside the protected function call.

### Verification checklist

1. Test allow, deny, pending approval, approval, rejection, changed arguments, replay, timeout, and
   post-effect response loss with synthetic downstream credentials.
2. Confirm every route to the payment, database, deployment, messaging, or other consequential
   client uses the returned `FunctionTool` and cannot call the original function directly.
3. Keep the policy, database, auth config, and downstream credentials outside model-writable paths.
4. Require downstream idempotency using the same business key as a second safety boundary.
5. Alert on `unknown`, `execution_abandoned`, invalid receipt chains, and binding conflicts.

The integration is credential-free in AgentBarrier's own test suite: it invokes real SDK tool
objects and contexts without making a model API request.
