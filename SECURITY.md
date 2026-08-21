# Security policy

## Supported versions

Security fixes are provided for the latest published `1.x` minor release. Older minors may require
an upgrade to receive a fix. Pre-1.0 releases are unsupported.

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
