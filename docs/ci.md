# Run AgentBarrier in CI

The most useful target is an application adapter that routes the application's real tool
registration and scheduling path into AgentBarrier's sentinel effect. Keep production credentials
out of the job and use synthetic action data.

## GitHub Actions

Copy this workflow to `.github/workflows/agentbarrier.yml` and replace
`myapp.agentbarrier_adapter:create_adapter` with the import path for your adapter. The job fails on
any failed or errored guarantee and preserves JSON and JUnit evidence even when verification fails.
First add AgentBarrier to the project's locked development dependencies and commit the updated
project and lock files:

```bash
uv add --dev agentbarrier
```

```yaml
name: AgentBarrier

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  verify-agent-controls:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@ae62891fec2bb8e7d6c99fc78c9fec3a63790f8d # v10.0.0
        with:
          enable-cache: true
      - run: uv sync --locked
      - run: mkdir -p build/agentbarrier
      - run: >-
          uv run
          agentbarrier verify myapp.agentbarrier_adapter:create_adapter
          --approval-profile run-wide
          --json build/agentbarrier/results.json
          --junit build/agentbarrier/results.xml
      - if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: agentbarrier-results
          path: build/agentbarrier/
          if-no-files-found: error
```

The locked dependency keeps the gate stable across framework releases. Add `--strict-skips` only
when the application adapter is expected to implement every capability; otherwise unsupported
guarantees remain visible skips without failing the job.

## Pytest

AgentBarrier installs a fixture automatically. This version fits projects that already use pytest:

```python
from myapp.agentbarrier_adapter import MyApplicationAdapter


def test_agent_control_boundaries(agentbarrier):
    result = agentbarrier.verify_sync(MyApplicationAdapter())
    result.raise_for_failure()
```

Run only the control-boundary test when diagnosing this gate:

```bash
pytest tests/test_agent_controls.py
```

To verify an application whose documented contract is per-action approval, configure the fixture
for that test invocation:

```bash
pytest --agentbarrier-profile per-action tests/test_agent_controls.py
```

The selected profile is retained in AgentBarrier's console, JSON, JUnit, and SARIF output.

## Reports

The CLI can write multiple report formats in one run:

```bash
agentbarrier verify myapp.agentbarrier_adapter:create_adapter \
  --approval-profile run-wide \
  --json build/agentbarrier/results.json \
  --junit build/agentbarrier/results.xml \
  --sarif build/agentbarrier/results.sarif
```

- JSON retains the complete structured scenario evidence.
- JUnit appears in CI test-report interfaces.
- SARIF can be uploaded to a code-scanning system when that system accepts custom results.

Do not connect the conformance job to production tools. The adapter should replace the final
effect boundary with the supplied `EffectProbe` while preserving the application's normal control
flow.

AgentBarrier's own package CI additionally runs the shared runtime-store contract against real
SQLite and PostgreSQL backends. A clean virtual environment installs the built wheel and proves
pending, approval, execution, replay, pause, limit, and receipt-chain behavior against PostgreSQL;
this guards against source-tree-only dependency or packaging mistakes. The core and live
PostgreSQL jobs each enforce at least 90 percent branch coverage in their respective environments.
