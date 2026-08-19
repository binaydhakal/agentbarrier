# Changelog

All notable changes to this project will be documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Reproducible terminal recording that demonstrates an unsafe pre-approval effect and the safe
  reference result for the same guarantee.
- Optional ANSI color for terminal reports through `--color auto|always|never`.
- Credential-free PydanticAI adapter covering approval, rejection, argument binding, cancellation,
  timeout, and strict parallel-barrier behavior.
- Credential-free Google Agent Development Kit adapter covering approval, rejection, cancellation,
  timeout, and strict parallel-barrier behavior.

### Changed

- Console failures now include the finding title, expected behavior, observed evidence, and
  remediation guidance.

## [0.1.0] - 2026-08-19

### Added

- Ten deterministic lifecycle guarantees covering approval, rejection, exact argument binding,
  replay, unknown outcomes, cancellation, timeout, parallel barriers, delegation, and audit
  receipts.
- External SQLite effect journal with durable, ordered evidence.
- Safe reference adapter and public adapter/run-handle contract.
- Credential-free OpenAI Agents Python and LangGraph framework adapters.
- Console, JSON, JUnit, and SARIF reports.
- CLI, pytest fixture, Python 3.10–3.13 support, and strict type checking.

[Unreleased]: https://github.com/binaydhakal/agentbarrier/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/binaydhakal/agentbarrier/releases/tag/v0.1.0
