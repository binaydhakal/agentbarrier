# MCP policy gateway

> The MCP gateway is under development for AgentBarrier 0.5.0. Pin an exact pre-1.0 version and
> keep the upstream server unreachable from untrusted clients while this contract evolves.

The gateway places AgentBarrier between an MCP client and an existing MCP server. It forwards tool
discovery, but every tool call crosses the same policy, approval, idempotency, and audit boundary as
a protected Python function.

```text
MCP client or agent
        |
        v
AgentBarrier gateway  ->  policy + SQLite approval/audit state
        |
        v
Existing MCP server  ->  payment, database, deployment, email, or another effect
```

The upstream server does not need AgentBarrier-specific code. The client changes its MCP endpoint
or stdio command so the gateway becomes the only route to the upstream credentials and tools.

## Install

```bash
python -m pip install 'agentbarrier[mcp] @ git+https://github.com/binaydhakal/agentbarrier.git'
```

After the 0.5.0 release, use `python -m pip install 'agentbarrier[mcp]'` from PyPI.

The integration uses the official MCP Python SDK 2.x. That SDK supports the stateless MCP
2026-07-28 revision and earlier handshake-era clients from the same server implementation.

## Write a policy

AgentBarrier uses the same strict policy document for Python and MCP tools. The first matching rule
wins, and an explicit deny default fails closed.

```json
{
  "version": "support-policy-v1",
  "default": "deny",
  "rules": [
    {
      "name": "deny database deletes",
      "effect": "deny",
      "tool": "database.delete"
    },
    {
      "name": "review large refunds",
      "effect": "require_approval",
      "tool": "payments.refund",
      "approval_ttl_seconds": 3600,
      "conditions": [
        {"path": "amount", "operator": "gt", "value": 20}
      ]
    },
    {
      "name": "allow small refunds",
      "effect": "allow",
      "tool": "payments.refund"
    }
  ]
}
```

Validate policy documents against `docs/schemas/runtime-policy-v1.schema.json`. Change the policy
version whenever a rule changes because the version is part of the exact action binding.

## Choose stable operation identity

An MCP JSON-RPC request ID identifies one protocol request; it is not durable business identity for
retries after a disconnect. AgentBarrier therefore never guesses an idempotency key.

Prefer a stable identifier already present in the tool arguments:

```bash
--idempotency-argument request_id
```

Nested fields use a dotted path such as `operation.identity.request_id`. If tools use different
argument shapes, integrate `MCPGateway` in Python and provide a per-tool
`MCPIdempotencyResolver`.

When no argument path is configured, the client must add this string to `params._meta`:

```json
{
  "agentbarrier/idempotencyKey": "refund-1001"
}
```

Reusing the key with a different organization, requester, namespace, tool, arguments, or policy
version is blocked before the upstream server is called. The downstream service should enforce the
same business key as a second layer because no proxy can provide exactly-once behavior after a
network failure unless the effect destination can reconcile that identity.

## Proxy a stdio server

The gateway speaks stdio to the client and launches the configured stdio server as its upstream:

```bash
agentbarrier mcp stdio \
  --policy policy.json \
  --db agentbarrier.db \
  --namespace support-agent \
  --organization acme \
  --requested-by support-agent \
  --upstream-command python \
  --upstream-arg server.py \
  --idempotency-argument request_id
```

Repeat `--upstream-arg` for every argument. Keep stdout reserved for MCP messages; operational logs
belong on stderr.

An HTTP upstream can also sit behind a stdio-facing gateway:

```bash
agentbarrier mcp stdio \
  --policy policy.json \
  --db agentbarrier.db \
  --upstream-url https://mcp.example.com/mcp
```

## Serve Streamable HTTP

Create an auth file using the format in the [approval API guide](approval-api.md). Give the agent
credential only `mcp:call`; do not reuse a reviewer token. The file stores SHA-256 digests of
high-entropy tokens, never plaintext credentials.

```bash
agentbarrier mcp http \
  --policy policy.json \
  --db agentbarrier.db \
  --namespace support-agent \
  --organization acme \
  --requested-by support-agent \
  --upstream-url https://mcp.example.com/mcp \
  --upstream-bearer-token-env MCP_UPSTREAM_TOKEN \
  --auth-config approval-auth.json \
  --host 127.0.0.1 \
  --port 8765 \
  --path /mcp \
  --max-request-bytes 1048576
```

The safe default listens only on `127.0.0.1` and limits request bodies to 1 MiB. A non-loopback
host is rejected unless `--auth-config` is present. Every HTTP request must then carry a valid
`Authorization: Bearer ...` credential with `mcp:call`; missing, invalid, and insufficient-scope
credentials receive 401 or 403 before MCP processing.

`--organization` and `--requested-by` bind a trusted service identity to every action created by
this gateway. A non-default organization requires an explicit requester. Use a version 2 auth file
for isolated review and requester/reviewer separation; see
[multi-user authorization](multi-user-authorization.md).

`--upstream-bearer-token-env` reads an upstream HTTP token from the named environment variable at
connection time. The value is validated, kept out of configuration files and command arguments,
and sent only in the Authorization header. Upstream redirects are disabled so a credential cannot
silently follow a redirect to another host. Remote upstreams must use HTTPS; plaintext HTTP is
accepted only for loopback development. Embedded URL credentials and URL fragments are rejected.
Use host allow-listing, rate limiting, and network policy at trusted ingress whenever traffic
leaves loopback.

## Approval flow

If a call requires review, the gateway returns an MCP tool error and includes the durable action
under the `agentbarrier/action` result metadata key. The upstream tool has not run.

```bash
agentbarrier approvals list --db agentbarrier.db --status pending
agentbarrier approvals show ACTION_ID --db agentbarrier.db --json
agentbarrier approvals approve ACTION_ID \
  --db agentbarrier.db \
  --decided-by alice \
  --reason ticket-123
```

After approval, retry the exact tool call with the same idempotency key. The gateway atomically
claims the action and forwards it once. Later retries return the stored MCP result without calling
the upstream server again.

Rejected, denied, expired, executing, and unknown actions also return `agentbarrier/action`
metadata with their current status. An idempotency binding violation returns
`agentbarrier/error.code = "action_binding_error"` without revealing the previously reviewed
arguments.

## Failure behavior

- Upstream progress notifications are forwarded to the requesting client after execution begins.
- Concurrent duplicates do not create a second upstream call.
- A reconnect can replay a completed result from SQLite.
- Cancellation propagates to the upstream SDK and marks the claimed action `unknown`.
- An upstream protocol or transport failure is preserved for the original caller and marks the
  claimed action `unknown`; retrying does not call upstream again.
- An `unknown` action requires downstream reconciliation before any new execution can occur.

These rules are intentionally conservative. Once the upstream call starts, an error response does
not prove that the external side effect failed to commit.

## Current development scope

The first 0.5.0 slice forwards `tools/list` and complete `tools/call` results. It preserves tool
schemas, structured content, result metadata, tool errors, progress, cancellation, and current MCP
version negotiation through the official SDK.

Raw stdio runs across a subprocess boundary, and raw Streamable HTTP fixtures cover scoped
authentication, initialization, discovery, execution, replay, malformed requests, and body-size
enforcement. Package CI installs the built wheel into a clean environment and proves MCP pending →
approved → executed → replayed behavior with one upstream effect.

Interactive `InputRequiredResult` tool rounds are not yet a supported execution contract. If an
upstream tool requests additional sampling, elicitation, or roots input after AgentBarrier has
claimed execution, the call fails closed as an `unknown` outcome and is not retried automatically.
Do not expose interactive consequential tools through this release; split input collection from
the final side-effecting tool or reconcile the unknown action before any retry.

## Complete-mediation checklist

1. Clients connect only to the gateway, never directly to the upstream server.
2. Upstream credentials are available only to the gateway service account.
3. Agent, reviewer, and audit identities use separate least-privilege bearer tokens.
4. Policy, auth, and SQLite files or PostgreSQL credentials are outside model-writable locations
   with restrictive permissions.
5. Every consequential call supplies a real business idempotency key.
6. Policy defaults to deny and policy changes use a new version.
7. Operators alert on `unknown`, binding violations, repeated denials, and a broken receipt chain.
8. The external system supports lookup or reconciliation using the same business key.

See the [runtime threat model](threat-model.md) for the broader trust boundary.

For multi-process gateways, replace `--db` with `--postgres-dsn-env NAME` and optionally select a
dedicated `--postgres-schema`. Provision it first using the [PostgreSQL guide](postgresql.md).
