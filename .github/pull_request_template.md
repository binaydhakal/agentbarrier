## Summary

Describe the control guarantee, adapter, or behavior changed by this pull request.

## Safety boundary

- What externally observed sentinel effect does this exercise?
- Why can the test not reach a production tool or credential?

## Evidence

List the safe and unsafe paths tested. For assertion changes, include the finding raised by the
intentionally unsafe path.

## Checklist

- [ ] I used only sentinel or disposable effects.
- [ ] I added or updated tests for the behavior changed.
- [ ] I documented compatibility or user-facing behavior where relevant.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run ruff format --check .` passes.
- [ ] `uv run mypy src` passes.
- [ ] `uv run pytest --cov=agentbarrier --cov-report=term-missing` passes.
- [ ] The change contains no credentials, private traces, or production tool arguments.
