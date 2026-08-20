<p align="center">
  <img
    src="https://raw.githubusercontent.com/binaydhakal/agentbarrier/main/docs/assets/agentbarrier-icon.png"
    alt="AgentBarrier icon"
    width="160"
  >
</p>

# AgentBarrier

<p align="center">
  <strong>Prove your AI agent cannot act after rejection, cancellation, timeout, or replay.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/agentbarrier/"><img src="https://img.shields.io/pypi/v/agentbarrier.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/agentbarrier/"><img src="https://img.shields.io/pypi/pyversions/agentbarrier.svg" alt="Python versions"></a>
  <a href="https://github.com/binaydhakal/agentbarrier/actions/workflows/ci.yml"><img src="https://github.com/binaydhakal/agentbarrier/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/agentbarrier.svg" alt="License"></a>
</p>

AgentBarrier is a deterministic test harness for the control guarantees around AI-agent tool
execution. It verifies that approval, rejection, cancellation, timeout, replay, delegation,
ambiguous outcomes, audit receipts, and parallel execution controls prevent unintended side
effects.

It does not judge model responses and does not need an API key. AgentBarrier invokes controlled
sentinel tools, observes their effects outside the agent framework, and reports whether the
framework or application honored the expected lifecycle boundary.

> **Status:** early development. The public adapter contract is usable, but compatibility should
> be pinned until the first stable release.

<p align="center">
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/adapters.md">Adapter guide</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/compatibility.md">Compatibility</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/ci.md">CI guide</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/docs/threat-model.md">Threat model</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/ROADMAP.md">Roadmap</a>
  ·
  <a href="https://github.com/binaydhakal/agentbarrier/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

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
```

The core, OpenAI, PydanticAI, Google ADK, and AutoGen adapters support Python 3.10–3.13. The
LangGraph adapter requires Python 3.11+ because its interrupt lifecycle relies on async
runnable-context propagation. Google ADK currently marks its tool-confirmation feature as
experimental, so its adapter may emit that upstream warning during verification.

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
| `cancellation` | `cancellation` | Work cancelled after it starts cannot commit later. |
| `timeout` | `timeout` | Timed-out work cannot commit later. |
| `parallel_barrier` | `parallel_barrier` | A pending approval holds sibling side effects. |
| `delegation` | `delegation` | Parent rejection prevents every delegated child effect. |
| `audit_receipts` | `audit_receipts` | Requests and decisions have complete, action-bound receipts. |

Unsupported capabilities are explicitly reported as skipped. They are never silently counted as
passing.

The strict `parallel_barrier` profile intentionally requires a pending approval to hold all sibling
side effects in the logical run. A framework may document a narrower, per-call approval contract;
in that case AgentBarrier still reports the difference rather than silently weakening the profile.

## Adapter contract

An adapter starts one or more `ActionRequest` objects using the supplied `EffectProbe` and returns
a `RunHandle`. The handle exposes pending actions and lifecycle decisions. See
`agentbarrier.adapters.reference.ReferenceAdapter` for the complete, safe implementation and
`docs/adapters.md` for implementation rules.

Current framework results are recorded in
[the compatibility matrix](https://github.com/binaydhakal/agentbarrier/blob/main/docs/compatibility.md).
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
uv build
uv run twine check dist/*
```

Good first contributions include framework adapters, application examples, and deterministic
reproductions of control failures. Start with the
[contribution guide](https://github.com/binaydhakal/agentbarrier/blob/main/CONTRIBUTING.md) or open a
[framework adapter request](https://github.com/binaydhakal/agentbarrier/issues/new?template=framework_adapter.yml).

## License

Apache-2.0
