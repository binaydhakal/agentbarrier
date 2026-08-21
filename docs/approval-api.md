# Approval HTTP API

> The approval API is under development for AgentBarrier 0.5.0. Bind it to loopback or a trusted
> private ingress until the complete deployment and authorization audit is finished.

The API lets an operator, internal service, or future dashboard inspect pending actions and record
approval decisions without direct shell access to the runtime database. Reviewer identity comes
from a scoped bearer credential and cannot be supplied or replaced by the request body.

## Install

Until 0.5.0 is released, install the service dependencies from the main branch:

```bash
python -m pip install 'agentbarrier[service] @ git+https://github.com/binaydhakal/agentbarrier.git'
```

After release, use `python -m pip install 'agentbarrier[service]'` from PyPI.

## Create a strong bearer credential

Generate a high-entropy random token with a password manager or cryptographic secret generator.
Do not place the plaintext token in shell history or in the auth file. Hash it from a hidden prompt:

```bash
agentbarrier auth hash-token
```

For non-interactive secret provisioning, pass only the environment variable name:

```bash
agentbarrier auth hash-token --token-env AGENTBARRIER_REVIEWER_TOKEN
```

The command prints the SHA-256 value and never prints the token. An unsalted SHA-256 value is safe
only for a high-entropy random token; a human password can be guessed offline.

## Configure identities and scopes

Create `approval-auth.json` outside model-writable directories:

```json
{
  "version": "1",
  "tokens": [
    {
      "subject": "reviewer@example.com",
      "token_sha256": "REPLACE_WITH_64_HEXADECIMAL_CHARACTERS",
      "scopes": ["actions:read", "actions:decide", "audit:read"]
    },
    {
      "subject": "audit-exporter",
      "token_sha256": "REPLACE_WITH_ANOTHER_64_CHARACTER_VALUE",
      "scopes": ["audit:read"]
    },
    {
      "subject": "mcp-agent",
      "token_sha256": "REPLACE_WITH_A_THIRD_64_CHARACTER_VALUE",
      "scopes": ["mcp:call"]
    }
  ]
}
```

The file is strict: unknown fields, duplicate token digests, duplicate scopes, invalid subjects,
and unknown scopes are rejected at startup.

| Scope | Access |
| --- | --- |
| `actions:read` | List actions and inspect exact action details. |
| `actions:decide` | Approve or reject a pending action. |
| `audit:read` | Read integrity-linked runtime receipts and chain status. |
| `mcp:call` | Connect to and call an authenticated AgentBarrier MCP HTTP gateway. |

A decision records the credential's `subject` as `decided_by`. There is deliberately no
`decided_by` request field. The approval API and MCP gateway may share this strict auth-file
format, but use separate least-privilege tokens for agents, reviewers, and audit exporters.

## Run on loopback

```bash
agentbarrier api \
  --db agentbarrier.db \
  --auth-config approval-auth.json
```

The default endpoint is `http://127.0.0.1:8787`. Use `--host` and `--port` only behind TLS and an
authenticated, rate-limited private ingress. The service does not enable browser CORS or cookies.

## Endpoints

| Method and path | Scope | Purpose |
| --- | --- | --- |
| `GET /health/ready` | Public | Process and runtime-schema readiness. |
| `GET /openapi.json` | Public | OpenAPI 3.1 service contract. |
| `GET /v1/actions` | `actions:read` | Filtered, cursor-paginated action list. |
| `GET /v1/actions/{action_id}` | `actions:read` | Exact arguments, policy, state, and result. |
| `POST /v1/actions/{action_id}/approve` | `actions:decide` | Approve the exact pending action. |
| `POST /v1/actions/{action_id}/reject` | `actions:decide` | Reject the exact pending action. |
| `GET /v1/audit` | `audit:read` | Paginated receipts and global chain validity. |

List pending actions:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $AGENTBARRIER_REVIEWER_TOKEN" \
  'http://127.0.0.1:8787/v1/actions?status=pending&limit=50'
```

Approve an action:

```bash
curl --fail-with-body \
  -X POST \
  -H "Authorization: Bearer $AGENTBARRIER_REVIEWER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"reason":"ticket-123"}' \
  http://127.0.0.1:8787/v1/actions/ACTION_ID/approve
```

Repeating the same approve or reject request is idempotent and does not add a second decision
receipt. Trying the opposite decision or deciding an incompatible state returns HTTP 409.

## Error and transport contract

Errors always use JSON:

```json
{
  "error": {
    "code": "insufficient_scope",
    "message": "the bearer token does not grant the required scope",
    "request_id": "8ab15532-33eb-4e88-b791-89d6a9ccb491"
  }
}
```

Every response includes `X-Request-Id`, `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, a deny-all content security policy, and a no-referrer policy.
Missing or invalid tokens return 401 with `WWW-Authenticate`; insufficient scope returns 403 with
the required scope. Decision bodies are limited to 16 KiB, accept only JSON, and reject unknown
fields.

## Deployment checklist

1. Generate random tokens with at least 128 bits of entropy and rotate them through a secret
   manager.
2. Store the auth file and runtime database outside model-writable paths with restrictive
   permissions.
3. Keep the listener on loopback unless a TLS-terminating, authenticated private ingress is in
   front of it.
4. Give automation only the minimum read, decision, or audit scopes it needs.
5. Do not log Authorization headers, action arguments, or returned results at ingress.
6. Rate-limit authentication failures and decision endpoints at the reverse proxy.
7. Alert on unknown outcomes, conflicting decisions, and an invalid receipt chain.

The static-token service is a secure single-node step, not the final multi-user identity system.
Organizations, role management, separation-of-duty policy, and external identity-provider support
remain 1.0 deliverables.
