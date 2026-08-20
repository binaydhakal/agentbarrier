# CrewAI adapter evaluation

This evaluation records model-free observations from CrewAI's real native tool lifecycle. It is
kept separate from the main compatibility artifact because CrewAI 1.15.17 requires OpenAI SDK
2.x, while the current OpenAI Agents adapter requires OpenAI SDK 3.x. The two framework extras
cannot be installed into one Python environment.

<!-- agentbarrier:compatibility:start -->
Canonical evidence: Python 3.11 · AgentBarrier 0.3.0.dev0 · `run-wide` profile

| Adapter | Version | Approval | Rejection | Args | Replay | Unknown | Reconcile | Cancel | Timeout | Parallel | Delegation | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CrewAI (isolated dependency environment) | 1.15.17 | Pass | Pass | Pass | — | — | — | — | — | **AB010** | — | — |
<!-- agentbarrier:compatibility:end -->

The generated table shows the default `run-wide` profile. `AB010` means CrewAI allowed the
ungated sibling tool to commit while another native tool call was waiting in the pre-tool hook.
The gated call itself remained effect-free, and the same adapter passes the narrower `per-action`
profile.

## Boundary under test

The adapter runs a real `Agent`, `Task`, and `Crew` with CrewAI's native function-calling path. A
local deterministic `BaseLLM` returns fixed native tool calls, so no model provider, API key, or
network request is involved. CrewAI parses each call, emits its tool-start event, invokes the
registered `before_tool_call` hook, and only then calls the sentinel tool.

The hook receives the parsed tool name and mutable argument dictionary. Returning `False` blocks
the callable; approving lets the same call proceed; approving replacement arguments edits that
dictionary in place before CrewAI invokes the tool. This is the actual framework boundary, not a
parallel simulation outside CrewAI.

The probe temporarily sets CrewAI's two documented telemetry-disable environment variables and
restores their prior values after every run. Tests also verify the adapter without model
credentials.

## Supported observations

- Approval holds the exact gated call before its sentinel effect.
- Rejection returns `False` from the real pre-tool hook and the sentinel does not execute.
- Edited approval arguments are the arguments CrewAI passes to the callable.
- CrewAI executes multiple native tool calls in its real worker pool. The gated call remains held,
  while an ungated sibling may continue; this passes `per-action` and produces stable finding
  `AB010` under `run-wide`.

## Explicitly unsupported

- **Cancellation and timeout fencing:** CrewAI executes parallel native tools with Python worker
  threads. Its runtime can cancel work that has not started, but it cannot interrupt a tool thread
  already in flight. The adapter therefore does not claim either capability.
- **Replay and ambiguous-outcome reconciliation:** this hook does not expose a stable resumable
  invocation or framework-owned idempotency/reconciliation contract.
- **Delegation inheritance and audit receipts:** the minimal tool-hook lifecycle does not provide
  evidence strong enough for those AgentBarrier capabilities.

These cases are recorded as skips, never inferred passes.

## Reproduce

```bash
uv run --isolated --extra test --extra crewai \
  pytest tests/test_crewai_adapter.py

uv run --isolated --extra test --extra crewai \
  agentbarrier compatibility \
  --adapter crewai \
  --json docs/crewai-evaluation.json \
  --markdown docs/crewai-evaluation.md \
  --strict-missing
```

The command runs both approval profiles. The JSON omits run identifiers, timestamps, durations,
temporary paths, model output, and credentials so repeated runs remain deterministic. CI repeats
the evaluation on Python 3.10–3.13 and checks the committed Python 3.11 artifact for drift.
