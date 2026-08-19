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
| Google ADK | 2.7.1 | Pass | Pass | — | — | — | Pass | Pass | **AB010** | — | — |
| AutoGen Core (single-threaded runtime) | 0.7.5 | Pass | Pass | Pass | — | — | Pass | Pass | Pass | — | — |

An em dash means the adapter does not declare that capability; the result is an explicit skip, not
a pass.

## AB010 observation

In the strict parallel scenario, the deterministic plan contains two sibling tool calls. One
requires approval and one does not. AgentBarrier expects the pending approval to hold the complete
logical run. On the tested OpenAI Agents, LangGraph, PydanticAI, and Google ADK versions, the
ungated sibling commits before the decision for the gated call. AutoGen Core's single-threaded
runtime instead holds message processing inside its intervention handler, so the sibling call
remains queued and the strict parallel scenario passes.

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

uv run agentbarrier verify \
  agentbarrier.adapters.google_adk:GoogleADKAdapter \
  --json build/google-adk.json

uv run agentbarrier verify \
  agentbarrier.adapters.autogen:AutoGenAdapter \
  --json build/autogen.json
```

Each run uses a temporary SQLite effect journal and a deterministic local plan. No prompt, model,
credential, network request, or production tool is involved. Google ADK 2.7.1 marks its
tool-confirmation feature as experimental and emits an upstream warning when that probe runs.
