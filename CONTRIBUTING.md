# Contributing

Contributions are welcome, especially application adapters, framework adapters, deterministic
failure reproductions, and improvements to the guarantee definitions.

Before starting a larger change, open a
[framework adapter request](https://github.com/binaydhakal/agentbarrier/issues/new?template=framework_adapter.yml)
or describe a safe
[control-failure reproduction](https://github.com/binaydhakal/agentbarrier/issues/new?template=control_finding.yml).
Usage questions belong in
[GitHub Discussions](https://github.com/binaydhakal/agentbarrier/discussions/categories/q-a).

## Development setup

```bash
git clone https://github.com/binaydhakal/agentbarrier.git
cd agentbarrier
uv sync --extra test --extra all
```

Run the complete local gate before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=agentbarrier --cov-report=term-missing
uv build
uv run twine check dist/*
```

## Adapter contributions

- Preserve the run and action identifiers supplied by the suite.
- Call the sentinel at the same boundary where the production effect would occur.
- Never invoke a real consequential tool from a conformance test.
- Declare only capabilities that the adapter can actually exercise.
- Add an integration test that uses no provider credential.
- Record tested versions and observed results in `docs/compatibility.md`.

Read `docs/adapters.md` before implementing an adapter.

The adapter does not need to implement every capability. Unsupported guarantees must be declared
honestly and remain visible as skipped; do not simulate support only to make the matrix green.

## Pull requests

Keep changes focused and include tests for both the safe path and at least one relevant failure
path. A passing self-test is not enough for changes to assertion logic: demonstrate that an unsafe
adapter produces the intended finding.

By contributing, you agree that your contribution is licensed under Apache-2.0.
