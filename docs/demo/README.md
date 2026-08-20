# Regenerating the terminal demo

The visual demo is generated from `failure.tape`; its failure is not edited or simulated.
The first command runs `python -m examples.run_unsafe_approval`, which checks the `approval_hold`
guarantee against the deliberately unsafe adapter in `examples/unsafe_approval.py`. The second
runs the same guarantee against AgentBarrier's safe reference adapter.

Install [VHS](https://github.com/charmbracelet/vhs), then run from the repository root:

```bash
vhs docs/demo/failure.tape
```

The command replaces `docs/assets/agentbarrier-demo.gif` with a fresh recording.
