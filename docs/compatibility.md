# Compatibility matrix

This matrix records model-free observations from pinned releases. A pass applies only to the
specific minimal probe described here. A failure is a reproducible difference from AgentBarrier's
strict guarantee; it is not, by itself, a vulnerability classification.

Last verified: 2026-08-19

| Adapter | Version | Approval | Rejection | Args | Replay | Unknown | Cancel | Timeout | Parallel | Delegation | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Reference | 0.1.0 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| OpenAI Agents Python | 0.22.0 | Pass | Pass | — | — | — | Pass | Pass | **AB010** | — | — |
| LangGraph (Python 3.11+) | 1.2.11 | Pass | Pass | Pass | — | — | Pass | Pass | **AB010** | — | — |
| PydanticAI | 2.32.0 | Pass | Pass | Pass | — | — | Pass | Pass | **AB010** | — | — |

An em dash means the adapter does not declare that capability; the result is an explicit skip, not
a pass.

## AB010 observation

In the strict parallel scenario, the deterministic plan contains two sibling tool calls. One
requires approval and one does not. AgentBarrier expects the pending approval to hold the complete
logical run. On the framework versions above, the ungated sibling commits before the decision for
the gated call.

This result is expected to evolve as frameworks change. CI tests the adapter contract without
requiring a particular framework to pass every guarantee, allowing fixes to turn a failure into a
pass without breaking AgentBarrier itself.

## Reproduce

```bash
uv sync --extra test --extra all

uv run agentbarrier verify \
  agentbarrier.adapters.openai_agents:OpenAIAgentsAdapter \
  --json build/openai-agents.json

uv run agentbarrier verify \
  agentbarrier.adapters.langgraph:LangGraphAdapter \
  --json build/langgraph.json

uv run agentbarrier verify \
  agentbarrier.adapters.pydantic_ai:PydanticAIAdapter \
  --json build/pydantic-ai.json
```

Each run uses a temporary SQLite effect journal and a deterministic local plan. No prompt, model,
credential, network request, or production tool is involved.
