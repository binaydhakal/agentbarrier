# Security policy

## Supported versions

Until the first stable release, only the latest published `0.x` release receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not open a public issue
for a vulnerability that could put users at risk.

Include:

- the affected AgentBarrier and framework versions;
- a minimal model-free reproduction;
- the expected and observed effect events;
- realistic impact; and
- any suggested mitigation.

AgentBarrier reports differences from explicit test guarantees. A failed guarantee is not
automatically a security vulnerability; impact depends on the application threat model and whether
the tested boundary completely mediates real effects.
