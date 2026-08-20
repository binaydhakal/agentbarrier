# Framework runtime integrations

Framework runtime integrations put AgentBarrier immediately around a framework tool's original
Python callable. They are different from the compatibility adapters in `agentbarrier.adapters`,
which exercise deterministic sentinel tools to test framework lifecycle guarantees.

The development integrations support OpenAI Agents Python and LangGraph. Other evaluated frameworks
remain 0.5.0 release work where their real callable boundary can be protected without weakening the
runtime contract.

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
from dataclasses import dataclass
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

## LangGraph

> This integration is under development for AgentBarrier 0.5.0 and currently targets
> `langgraph>=1.2,<2` on Python 3.11+. Pin an exact pre-2.0 version and run application-specific
> tests before connecting consequential tools.

LangGraph's `ToolNode` executes model-requested tools and injects runtime state that is hidden from
the model-facing schema. AgentBarrier's `runtime_structured_tool` returns a normal LangChain Core
`StructuredTool` while placing policy, durable approval, exact argument binding, execution claims,
result replay, and receipts around the original callable. The companion `runtime_tool_node` turns
off exception-to-message conversion so a pending approval or uncertain effect reaches the host
application instead of looking like an ordinary tool response. See LangChain's official
[tools and ToolNode guide](https://docs.langchain.com/oss/python/langchain/tools).

Install the optional integration:

```bash
python -m pip install 'agentbarrier[langgraph]'
```

Build protected tools into a custom graph:

```python
from typing import Any

from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolRuntime

from agentbarrier.integrations.langgraph import runtime_structured_tool, runtime_tool_node
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, SQLiteRuntimeStore


@dataclass
class Context:
    payment_client: Any


async def refund_payment(
    request_id: str,
    amount: int,
    runtime: ToolRuntime[Context],
) -> dict[str, object]:
    """Refund one exact payment request."""
    payment_client = runtime.context.payment_client
    return await payment_client.refund(request_id=request_id, amount=amount)


store = SQLiteRuntimeStore("agentbarrier.db")
barrier = RuntimeBarrier(
    policy=RuntimePolicy.from_file("policy.json"),
    store=store,
    namespace="support-agent",
)
refund_tool = runtime_structured_tool(
    refund_payment,
    barrier=barrier,
    idempotency_key="request_id",
    name="payments_refund",
    description="Refund one exact payment request.",
)

builder = StateGraph(MessagesState, context_schema=Context)
builder.add_node("tools", runtime_tool_node([refund_tool]))
builder.add_edge(START, "tools")
builder.add_edge("tools", END)
graph = builder.compile()
```

In a normal agent loop, route the model's tool calls into this node and route successful tool
messages back to the model. Keep the store open for the graph service's complete lifetime. The
first gated call raises `ApprovalRequired` before the original function runs. After a reviewer
approves the action through the authenticated API, retry or resume with the same business
`request_id` and exact arguments. A different LangGraph/model tool-call ID is expected and does not
change the durable operation identity.

### Injected runtime and exact arguments

`ToolRuntime`, injected state, stores, configuration, and callback values remain available to the
original function but are omitted from the policy arguments because LangGraph hides them from the
tool-call schema. Defaults for model-visible function arguments are applied and included. Use a
top-level string argument or selector callable for `idempotency_key`; never use
`runtime.tool_call_id`, thread ID, or run ID as business identity.

Custom `args_schema` and `infer_schema=False` are rejected. Inferring the schema from the original
signature prevents a custom schema from hiding a model-controlled effect argument from policy.

### Fail-closed graph execution

Use `runtime_tool_node` for consequential protected tools. LangGraph supports converting selected
tool exceptions into `ToolMessage` objects, but that is unsafe after an execution claim: a network
error may mean the external effect committed even though its response was lost. The safe node
therefore propagates every execution exception to the host. AgentBarrier records the action as
`unknown`, and the host must reconcile it from downstream evidence before any retry.

Do not place a catch-all `wrap_tool_call` middleware around protected tools, construct another
`ToolNode` with `handle_tool_errors=True`, or turn the exception into model-visible content. Handle
`ApprovalRequired`, rejection, denial, binding conflict, and unknown outcomes outside the agent
loop. Validation errors that occur before the protected callable may be handled separately only if
the application can prove the effect boundary was never entered.

The protected callable's result must be JSON-compatible. `response_format="content_and_artifact"`,
generator functions, async-generator functions, opaque callable objects, and tool-returned graph
`Command` objects are not supported at this boundary. Perform graph-only state updates after a
successful protected result.

As with the OpenAI integration, complete mediation is an application responsibility: every route
to the consequential client must use the returned tool, while the original callable and downstream
credentials remain inaccessible to model-controlled code.
