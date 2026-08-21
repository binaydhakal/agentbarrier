# Framework runtime integrations

Framework runtime integrations put AgentBarrier immediately around a framework tool's original
Python callable. They are different from the compatibility adapters in `agentbarrier.adapters`,
which exercise deterministic sentinel tools to test framework lifecycle guarantees.

The stable integrations support OpenAI Agents Python, LangGraph, PydanticAI, and Google Agent
Development Kit where their real callable boundary can be protected without weakening the runtime
contract.

## OpenAI Agents Python

> This integration targets `openai-agents>=0.22,<1`. Pin compatible AgentBarrier and framework
> versions and run application-specific tests before connecting consequential tools.

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

> This integration targets `langgraph>=1.2,<2` on Python 3.11+. Pin compatible AgentBarrier and
> framework versions and run application-specific tests before connecting consequential tools.

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

## PydanticAI

> This integration targets `pydantic-ai-slim>=2.32,<3` on Python 3.10–3.13. Pin compatible
> AgentBarrier and framework versions and run application-specific tests before connecting
> consequential tools.

PydanticAI's `Tool` class derives model-visible parameters from a Python function and hides its
injected `RunContext`. AgentBarrier's `runtime_tool` returns that normal `Tool`, while policy,
approval, exact binding, execution claims, result replay, and receipts surround the original async
callable. See Pydantic's official [function tools guide](https://pydantic.dev/docs/ai/tools-toolsets/tools/).

Install the optional integration:

```bash
python -m pip install 'agentbarrier[pydantic-ai]'
```

Build and register a protected tool:

```python
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, RunContext

from agentbarrier.integrations.pydantic_ai import runtime_tool
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, SQLiteRuntimeStore


@dataclass
class Dependencies:
    payment_client: Any


async def refund_payment(
    context: RunContext[Dependencies],
    request_id: str,
    amount: int,
) -> dict[str, object]:
    """Refund one exact payment request."""
    return await context.deps.payment_client.refund(request_id=request_id, amount=amount)


store = SQLiteRuntimeStore("agentbarrier.db")
barrier = RuntimeBarrier(
    policy=RuntimePolicy.from_file("policy.json"),
    store=store,
    namespace="support-agent",
)
refund_tool = runtime_tool(
    refund_payment,
    barrier=barrier,
    idempotency_key="request_id",
    name="payments_refund",
    description="Refund one exact payment request.",
)
agent = Agent(
    "openai:gpt-5.4",
    deps_type=Dependencies,
    tools=[refund_tool],
)
```

Keep the store open for the service lifetime. A gated call raises AgentBarrier's
`ApprovalRequired` to the host before `refund_payment` runs. After an authenticated reviewer
approves the durable action, retry or resume using the same `request_id` and exact arguments. A new
PydanticAI `context.tool_call_id`, run ID, or conversation ID does not change business identity.

### Async and fail-closed settings

The integration accepts async functions only. PydanticAI normally moves synchronous tools to a
worker thread; cancelling the agent cannot reliably stop a thread from committing a late effect.
Async alone is not magic—the function must still use cancellation-aware I/O and must not hide
blocking work—but it allows host cancellation to reach AgentBarrier and produce durable `unknown`
state when completion cannot be proven.

Several PydanticAI controls are fixed or rejected:

- `requires_approval` remains false so AgentBarrier is the only approval authority.
- `timeout` remains `None`; PydanticAI converts a per-tool timeout into `ModelRetry`, which could ask
  the model to repeat an uncertain effect. Apply the timeout outside `agent.run()` instead.
- `max_retries` is fixed to zero. Validation and application-specific retry decisions belong before
  the consequential boundary.
- `prepare`, custom `function_schema`, explicit `takes_ctx`, and custom `schema_generator` are
  rejected because they can rename the tool or make the model schema differ from the arguments
  reviewed by policy.

Do not place the returned tool inside a `FunctionToolset` with a toolset-level timeout. Do not use
execution hooks that convert protected-tool exceptions into model-visible results. Argument
validators run before the protected callable and may reject malformed input, but they must not
perform consequential effects themselves.

If the original callable raises PydanticAI's `ModelRetry`, `ToolFailed`, `ApprovalRequired`, or
`CallDeferred` after AgentBarrier claims the action, the integration raises
`FrameworkControlSignalError` instead. The action is recorded as `unknown`, and the host must
reconcile it from downstream evidence. This prevents a framework recovery signal from silently
becoming a model retry or a second approval system.

The tool's parameters and result must be JSON-compatible with AgentBarrier's durable store. The
current integration requires one schema property per original non-context argument; PydanticAI's
flattened single-model-parameter schema is rejected. Generator functions, async-generator
functions, opaque callable objects, and synchronous functions are also rejected.

The test suite runs a real PydanticAI `Agent`, `Tool`, `RunContext`, and credential-free
`FunctionModel` through approval, execution, replay, binding conflict, cancellation, post-claim
failure, and suppressed framework retry. Complete mediation still requires the application to keep
the original callable and downstream credentials outside every model-controlled route.

## Google Agent Development Kit

> This integration targets `google-adk>=2.7,<3` on Python 3.10–3.13. Pin compatible AgentBarrier and
> framework versions and run application-specific tests before connecting consequential tools.

Google ADK automatically turns Python functions into `FunctionTool` objects, generates their model
schema from the signature, and injects a type-annotated `ToolContext` without exposing it to the
model. AgentBarrier's `runtime_function_tool` returns that normal ADK tool while policy, approval,
exact binding, execution claims, replay, and receipts surround the original async callable. See
Google's official [function tools guide](https://adk.dev/tools-custom/function-tools/).

Install the optional integration:

```bash
python -m pip install 'agentbarrier[google-adk]'
```

Build and register a protected function tool:

```python
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import ToolContext

from agentbarrier.integrations.google_adk import runtime_function_tool
from agentbarrier.runtime import RuntimeBarrier, RuntimePolicy, SQLiteRuntimeStore


async def refund_payment(
    request_id: str,
    amount: int,
    tool_context: ToolContext,
) -> dict[str, object]:
    """Refund one exact payment request."""
    payment_client: Any = tool_context.state["payment_client"]
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
    name="payments_refund",
    description="Refund one exact payment request.",
)
agent = Agent(
    name="support_agent",
    model="gemini-2.5-flash",
    tools=[refund_tool],
)
```

Keep the store open for the ADK service lifetime. A gated call raises AgentBarrier's
`ApprovalRequired` to the runner host before `refund_payment` runs. After an authenticated reviewer
approves the action, retry or resume with the same `request_id` and exact arguments. A new
`tool_context.function_call_id`, invocation ID, session ID, or model turn does not change the
durable operation identity.

### Async and fail-closed settings

The integration accepts async functions only. ADK runs synchronous functions in worker threads,
which cannot be reliably stopped by cancellation. Streaming `input_stream` tools, variadic or
positional-only parameters, generators, async generators, and opaque callable objects are also
rejected because their effect can escape the inspected callable boundary or their values are not
represented in ADK's model schema.

ADK native `require_confirmation` remains false. AgentBarrier must be the only approval authority,
because its decision is bound to durable business identity, canonical arguments, policy version,
and an atomic execution claim. Use a stable application-controlled key such as a payment request,
outbox record, deployment request, or message ID. Do not use `ToolContext.function_call_id`.

ADK's tool callbacks are powerful enough to skip a tool, replace its result, mutate its arguments,
or suppress an exception. The official
[callback guide](https://adk.dev/callbacks/types-of-callbacks/) says that a non-`None`
`on_tool_error_callback` result becomes the tool result instead of propagating the exception. For
protected tools:

- `before_tool_callback` may validate or mutate arguments, but it must not execute the
  consequential operation itself. AgentBarrier evaluates the final arguments passed to the tool.
- A `before_tool_callback` result skips the protected tool entirely. Do not use that path to return
  a cached consequential result unless the cache has the same durable binding guarantees.
- `on_tool_error_callback` must return `None` for AgentBarrier and post-claim application
  exceptions so approval, denial, binding conflict, and unknown outcomes reach the host.
- `after_tool_callback` must not reinterpret an uncertain operation as success. It runs only after
  the protected tool returns, so use it only for display-safe post-processing.

Missing required arguments are rejected by ADK before the protected callable is entered. The
original function's model-controlled parameters and result must be JSON-compatible with
AgentBarrier's store. Async code must use cancellation-aware I/O and must not hide blocking worker
work that can commit after cancellation.

The test suite and clean-wheel audit invoke a real ADK `FunctionTool` and injected `ToolContext`
through approval, execution, replay, binding conflict, cancellation, and post-claim failure without
model credentials. Complete mediation still requires every route to the consequential client to
use the returned tool and keep the original callable and credentials outside model-controlled code.
