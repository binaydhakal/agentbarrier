# Roadmap

AgentBarrier's roadmap is organized around one outcome: make control-plane safety regressions easy
to reproduce locally and enforce in CI without model credentials.

## 0.2.0 — broader framework coverage

- [x] Add a PydanticAI adapter and credential-free integration tests.
- [x] Add a Google Agent Development Kit adapter and credential-free integration tests.
- [x] Add an AutoGen adapter and credential-free integration tests.
- [x] Publish a reproducible visual demonstration of a real pre-approval side effect.
- [x] Make console failures explain the expected boundary, observed effect, and repair direction.
- [x] Expand the compatibility matrix with tested package and Python versions.
- [x] Add copy-ready CI examples for application adapters.

## Next

- Add a CrewAI adapter after its control lifecycle can be exercised deterministically.
- Add reusable profiles for per-action and strict run-wide approval barriers.
- Improve reconciliation tests for ambiguous post-commit outcomes.
- Add machine-readable compatibility evidence generated from integration tests.
- Collect real application-adapter examples from messaging, payments, databases, and deployments.

## How work is prioritized

A proposed feature moves up the roadmap when it:

1. catches a consequential side effect that ordinary unit tests commonly miss;
2. can be tested deterministically without a paid model call or production credential;
3. works at the real effect boundary instead of inferring safety from configuration; and
4. produces actionable evidence that a team can run in CI.

Framework requests and control-failure reproductions are welcome through the repository's issue
forms. Security-sensitive reports should follow [SECURITY.md](SECURITY.md) instead of a public
issue.
