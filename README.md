<p align="center">
  <img
    src="https://raw.githubusercontent.com/binaydhakal/agentbarrier/main/docs/assets/agentbarrier-icon.png"
    alt="AgentBarrier icon"
    width="160"
  >
</p>

# AgentBarrier

<p align="center">
  <strong>Enforce and prove safe approval boundaries for AI-agent actions.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/agentbarrier/"><img src="https://img.shields.io/pypi/v/agentbarrier.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/agentbarrier/"><img src="https://img.shields.io/pypi/pyversions/agentbarrier.svg" alt="Python versions"></a>
  <a href="https://github.com/binaydhakal/agentbarrier/actions/workflows/ci.yml"><img src="https://github.com/binaydhakal/agentbarrier/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/agentbarrier.svg" alt="License"></a>
</p>

AgentBarrier is an open-source policy gateway and approval control plane for AI-agent tool calls.
It sits immediately before a consequential action, decides whether to allow, deny, or pause the
exact call for review, prevents duplicate execution, and records what happened outside the model's
context.

Use it around Python tools today or run the development MCP gateway in front of an existing tool
server. The same project also includes a deterministic test suite for approval, rejection,
cancellation, timeout, replay, delegation, ambiguous outcomes, audit receipts, and parallel
execution controls. It does not ask a model to judge another model and does not require a model API
key.

Runtime enforcement is available in 0.4.0. It applies deterministic allow, deny, and approval rules
directly around synchronous and asynchronous Python tool functions, persists exact approval state
in SQLite, prevents duplicate execution, and emits integrity-linked audit receipts. See the
[runtime guide](https://github.com/binaydhakal/agentbarrier/blob/main/docs/runtime.md).
The [runtime API reference](https://github.com/binaydhakal/agentbarrier/blob/main/docs/runtime-api.md)
documents the public classes, lifecycle, and failure contract.
The main branch is developing 0.5.0, which adds a deployable
[MCP policy gateway](https://github.com/binaydhakal/agentbarrier/blob/main/docs/mcp-gateway.md)
using the current MCP 2026-07-28 protocol through its official Python SDK, an authenticated
approval API, and [durable signed webhooks](docs/webhooks.md) for approval and operations systems.

> **Status:** early development. The public adapter contract is usable, but compatibility should
> be pinned until the first stable release.

<p align="center">
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/adapters.md">Adapter guide</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/mcp-gateway.md">MCP gateway</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/approval-api.md">Approval API</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/webhooks.md">Webhooks</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/framework-runtime.md">Framework runtime</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/compatibility.md">Compatibility</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/ci.md">CI guide</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/payment-ledger-example.md">Payment example</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/threat-model.md">Threat model</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/ROADMAP.md">Roadmap</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

## What this protects in a real application

- a support agent issuing a refund above a configured amount;
- a coding agent deploying, deleting infrastructure, or applying a privileged change;
- a database assistant running writes while ordinary reads remain automatic;
- a communications agent sending external email or publishing content; and
- any MCP client calling a consequential tool hosted by an existing MCP server.

The model can propose the action, but it cannot approve its own request, change the reviewed
arguments afterward, or cause the same approved operation to execute twice through a retry.

## Run as an MCP safety gateway

Until 0.5.0 is released, install the optional gateway dependencies from the main branch:

```bash
python -m pip install 'agentbarrier[mcp] @ git+https://github.com/binaydhakal/agentbarrier.git'
```

Place AgentBarrier between an MCP client and an existing stdio server:

```bash
agentbarrier mcp stdio \
  --policy policy.json \
  --db agentbarrier.db \
  --upstream-command python \
  --upstream-arg server.py \
  --idempotency-argument request_id
```

Or expose a local Streamable HTTP gateway in front of a remote MCP endpoint:

```bash
agentbarrier mcp http \
  --policy policy.json \
  --db agentbarrier.db \
  --upstream-url https://mcp.example.com/mcp
```

The HTTP listener binds to `127.0.0.1:8765` by default. Each call must supply stable business
identity through the configured argument path or the `agentbarrier/idempotencyKey` MCP metadata
field. See the [MCP gateway guide](docs/mcp-gateway.md) for the policy example, approval flow,
security boundary, and current development limitations.

The same runtime database can be reviewed from the development authenticated HTTP service. It uses
scoped bearer identities, takes the reviewer name from authentication rather than request data, and
serves an OpenAPI 3.1 contract. See the [approval API guide](docs/approval-api.md).

Durable outbound webhooks can notify a separate approval UI, queue, SIEM, or incident workflow.
They use HMAC-SHA256 signatures, automatic and configured secret redaction, bounded retries,
crash-safe claims, stable event IDs, and explicit dead-letter recovery. See the
[signed webhook guide](docs/webhooks.md).

OpenAI Agents Python, LangGraph, PydanticAI, and Google ADK applications can construct normal
framework tools whose original Python callables are protected by the same durable runtime boundary.
Injected framework context is kept out of the reviewed business arguments, and fail-closed
execution settings prevent an approval or uncertain outcome from becoming ordinary model-visible
tool output. See the [framework runtime guide](docs/framework-runtime.md).

## See a real control failure

The first run below uses an intentionally unsafe adapter that commits while approval is still
pending. AgentBarrier catches the real sentinel effect as `AB002`. The second run exercises the
safe reference adapter and passes the same guarantee.

<p align="center">
  <img
    src="https://raw.githubusercontent.com/binaydhakal/agentbarrier/main/docs/assets/agentbarrier-demo.gif"
    alt="AgentBarrier detects an effect committed before approval, then passes the safe reference adapter"
    width="100%"
  >
</p>

<p align="center">
  <sub>The failure is produced by a real sentinel commit. <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/demo/failure.tape">View the reproducible recording source.</a></sub>
</p>

## Why

An agent that can send a message, issue a refund, modify a database, or deploy code needs stronger
evidence than a configuration flag named `requires_approval`. AgentBarrier tests the behavior at
the effect boundary:

- no effect before approval;
- no effect after rejection;
- approval is bound to the exact reviewed arguments;
- replay does not execute the same action twice;
- a lost post-commit response is reported as unknown and reconciled before retry;
- cancelled and timed-out work cannot commit later;
- a pending approval can hold sibling effects under the strict run-barrier profile;
- delegated work inherits its parent's rejection; and
- approval decisions produce action-digest-bound receipts.

## Quick start

```bash
python -m pip install agentbarrier
agentbarrier self-test
```

The self-test runs every guarantee against AgentBarrier's safe reference adapter. Application and
framework adapters implement the small `AgentAdapter` / `RunHandle` contract.

Use AgentBarrier in CI when your agent can cross a consequential boundary such as sending a
message, issuing a refund, changing a database, deploying code, or invoking another agent.

```python
from agentbarrier import SuiteRunner
from myapp.agentbarrier_adapter import MyApplicationAdapter

result = SuiteRunner().verify_sync(MyApplicationAdapter())
result.raise_for_failure()
```

### SQLite database boundary example

The repository includes a credential-free
[SQLite payment-ledger example](https://github.com/binaydhakal/agentbarrier/blob/main/docs/payment-ledger-example.md)
with intentionally unsafe and safe adapters. It verifies real local balance and transaction state
across approval, rejection, replay, response loss, cancellation, and timeout—without presenting the
example as production payment code.

```bash
uv run python -m examples.run_payment_ledger
```

### Approval-barrier profiles

The default `run-wide` profile requires any pending approval to hold every sibling effect in the
logical run. Select `per-action` when the intended contract allows ungated siblings to continue but
still requires the gated action itself to remain effect-free until approval.

```python
from agentbarrier import ApprovalBarrierProfile, RunnerOptions, SuiteRunner

runner = SuiteRunner(RunnerOptions(approval_profile=ApprovalBarrierProfile.PER_ACTION))
result = runner.verify_sync(MyApplicationAdapter())
result.raise_for_failure()
```

The equivalent CLI option is `--approval-profile per-action`. A stricter run-wide adapter also
passes the per-action profile; the profile chooses the minimum contract being tested, not how an
adapter must schedule its work.

### Framework probes

The built-in probes use deterministic local plans. They do not call a model provider or require an
API key.

```bash
python -m pip install 'agentbarrier[openai]'
agentbarrier verify agentbarrier.adapters.openai_agents:OpenAIAgentsAdapter

python -m pip install 'agentbarrier[langgraph]'
agentbarrier verify agentbarrier.adapters.langgraph:LangGraphAdapter

python -m pip install 'agentbarrier[pydantic-ai]'
agentbarrier verify agentbarrier.adapters.pydantic_ai:PydanticAIAdapter

python -m pip install 'agentbarrier[google-adk]'
agentbarrier verify agentbarrier.adapters.google_adk:GoogleADKAdapter

python -m pip install 'agentbarrier[autogen]'
agentbarrier verify agentbarrier.adapters.autogen:AutoGenAdapter

python -m pip install 'agentbarrier[crewai]'
agentbarrier verify agentbarrier.adapters.crewai:CrewAIAdapter \
  --approval-profile per-action
```

The core, OpenAI, PydanticAI, Google ADK, AutoGen, and CrewAI adapters support Python 3.10–3.13. The
LangGraph adapter requires Python 3.11+ because its interrupt lifecycle relies on async
runnable-context propagation. Google ADK currently marks its tool-confirmation feature as
experimental, so its adapter may emit that upstream warning during verification.

CrewAI is installed and tested separately from the `all` extra because its current OpenAI SDK 2.x
requirement conflicts with OpenAI Agents' 3.x requirement. Its real pre-tool hook enforces
per-action approval, rejection, and argument binding; CrewAI's threaded tools do not provide a
safe cancellation or timeout fence. See the
[reproducible CrewAI evaluation](https://github.com/binaydhakal/agentbarrier/blob/main/docs/crewai-evaluation.md).

These probes measure the framework's lifecycle behavior in a minimal configuration. For production
confidence, implement an application adapter that replaces your real consequential tools with the
sentinel at dependency-injection time. See
[the adapter guide](https://github.com/binaydhakal/agentbarrier/blob/main/docs/adapters.md).

The same runner is available as a pytest fixture:

```python
def test_agent_controls(agentbarrier):
    result = agentbarrier.verify_sync(MyApplicationAdapter())
    result.raise_for_failure()
```

## CLI reports

```bash
agentbarrier verify myapp.agentbarrier_adapter:create_adapter \
  --approval-profile run-wide \
  --json build/agentbarrier.json \
  --junit build/agentbarrier.xml \
  --sarif build/agentbarrier.sarif
```

The target may be an adapter instance, adapter class, or zero-argument factory. A non-zero exit
status is returned for failed or errored guarantees. `--strict-skips` also treats unsupported
guarantees as a failure. See the
[CI guide](https://github.com/binaydhakal/agentbarrier/blob/main/docs/ci.md) for copy-ready GitHub
Actions and pytest examples.

## Guarantees

| Scenario | Capability | Guarantee |
| --- | --- | --- |
| `approval_hold` | `approval` | No effect commits before approval; one commits afterward. |
| `rejection` | `rejection` | Rejected actions never commit. |
| `argument_binding` | `argument_binding` | Executed arguments exactly match approved arguments. |
| `replay` | `replay` | Replaying a completed action does not commit it twice. |
| `outcome_ambiguity` | `outcome_ambiguity` | A lost post-commit response becomes `UNKNOWN` and is not retried blindly. |
| `outcome_reconciliation` | `outcome_reconciliation` | Bounded identity lookup distinguishes committed, absent, conflicting, and unavailable evidence. |
| `cancellation` | `cancellation` | Work cancelled after it starts cannot commit later. |
| `timeout` | `timeout` | Timed-out work cannot commit later. |
| `parallel_barrier` | `parallel_barrier` | Parallel effects follow the selected approval-barrier profile. |
| `delegation` | `delegation` | Parent rejection prevents every delegated child effect. |
| `audit_receipts` | `audit_receipts` | Requests and decisions have complete, action-bound receipts. |

Unsupported capabilities are explicitly reported as skipped. They are never silently counted as
passing.

The default `run-wide` profile intentionally requires a pending approval to hold all sibling side
effects in the logical run. The `per-action` profile allows ungated siblings to proceed while the
gated action remains held. Reports always record the selected profile so a passing result cannot
silently change meaning.

## Adapter contract

An adapter starts one or more `ActionRequest` objects using the supplied `EffectProbe` and returns
a `RunHandle`. The handle exposes pending actions and lifecycle decisions. See
`agentbarrier.adapters.reference.ReferenceAdapter` for the complete, safe implementation and
`docs/adapters.md` for implementation rules.

Current framework results are recorded in
[the compatibility matrix](https://github.com/binaydhakal/agentbarrier/blob/main/docs/compatibility.md).
The same probe runs also produce
[versioned JSON evidence](https://github.com/binaydhakal/agentbarrier/blob/main/docs/compatibility.json)
that CI checks against the rendered table and uploads for every supported Python version.
The security boundary and limitations are defined in
[the threat model](https://github.com/binaydhakal/agentbarrier/blob/main/docs/threat-model.md).
Planned adapters and release priorities are public in
[the roadmap](https://github.com/binaydhakal/agentbarrier/blob/main/ROADMAP.md).

## Safety

Sentinel tools write only to a temporary SQLite journal owned by the test run. They do not call a
real API or modify production data. Do not replace a sentinel with a production tool when writing
an adapter.

## Research context

AgentBarrier is motivated by research showing that approval, cancellation, timeout, and replay
controls can leak side effects across agent frameworks. The initial scenario vocabulary follows
the failure classes in *Stop Means Stop: Measuring and Repairing the Enforcement Gap in
Agent-Framework Control Primitives* (2026): <https://arxiv.org/abs/2607.14166>.

## Development

```bash
uv sync --extra test --extra all
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=agentbarrier --cov-report=term-missing
uv run --isolated --extra test --extra crewai pytest tests/test_crewai_adapter.py
uv build
uv run twine check dist/*
```

Good first contributions include framework adapters, application examples, and deterministic
reproductions of control failures. Start with the
[contribution guide](https://github.com/binaydhakal/agentbarrier/blob/main/CONTRIBUTING.md) or open a
[framework adapter request](https://github.com/binaydhakal/agentbarrier/issues/new?template=framework_adapter.yml).

## License

Apache-2.0
