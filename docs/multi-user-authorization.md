# Multi-user authorization

AgentBarrier can isolate actions by organization and namespace, resolve permissions from reusable
roles, and require a different identity to review an action than the identity that requested it.
These checks protect the approval API and dashboard and are repeated inside the same database
transaction that records the decision.

## Label protected actions

Every production action should carry an organization and requester. For Python tools:

```python
barrier = RuntimeBarrier(
    policy=policy,
    store=store,
    namespace="billing",
    organization_id="acme",
    requested_by="refund-agent",
)
```

For an MCP gateway, use the equivalent service identity:

```bash
agentbarrier mcp http \
  --policy policy.json \
  --db agentbarrier.db \
  --namespace billing \
  --organization acme \
  --requested-by refund-agent \
  --upstream-url https://mcp.example.com/mcp \
  --auth-config mcp-auth.json
```

An organization-scoped MCP gateway refuses to start without `--requested-by`. The configured value
identifies the gateway service, not an arbitrary value supplied by the model. Applications that
need per-end-user attribution should construct a barrier with the authenticated application
identity for each request.

The organization, requester, namespace, exact tool arguments, idempotency key, and policy version
are bound to the action digest. Existing actions created without organization support remain in
the `default` organization and retain their original digests after migration.

## Configure organizations and roles

Store only hashes of high-entropy bearer tokens in the version 2 auth file:

```json
{
  "version": "2",
  "organizations": [
    {
      "id": "acme",
      "namespaces": ["billing", "support"],
      "require_separate_approver": true
    }
  ],
  "roles": [
    {
      "id": "reviewer",
      "scopes": ["actions:read", "actions:decide", "audit:read"],
      "decisions": ["approve", "reject"]
    },
    {
      "id": "auditor",
      "scopes": ["actions:read", "audit:read"],
      "decisions": []
    },
    {
      "id": "agent-runtime",
      "scopes": ["mcp:call"],
      "decisions": []
    }
  ],
  "tokens": [
    {
      "subject": "alice",
      "kind": "user",
      "organization": "acme",
      "roles": ["reviewer"],
      "token_sha256": "REPLACE_WITH_64_HEXADECIMAL_CHARACTERS"
    },
    {
      "subject": "refund-agent",
      "kind": "service",
      "organization": "acme",
      "roles": ["agent-runtime"],
      "token_sha256": "REPLACE_WITH_ANOTHER_64_CHARACTER_VALUE"
    }
  ]
}
```

Namespace ownership is exclusive: the same namespace cannot belong to two organizations in one
auth file. This makes namespaces durable tenant partitions while keeping action identifiers and
receipt chains globally unambiguous. A role may grant only approve, only reject, both decisions,
or neither; a role with decisions must also grant `actions:decide`.

## Enforcement behavior

- Action lists, detail pages, dashboard controls, and audit receipts are filtered to the
  authenticated organization and its namespaces.
- Cross-organization and out-of-namespace action IDs return not found, preventing object discovery.
- Decision routes independently verify the requested approve or reject permission.
- With `require_separate_approver`, an identity whose subject equals `requested_by` cannot approve
  or reject its own action.
- The runtime store repeats organization, namespace, decision, and self-review checks while holding
  the decision transaction lock. Calling code cannot pass a stale pre-check and race the decision.
- The authenticated token subject becomes `decided_by`; request bodies cannot replace it.

The original version 1 token format remains supported for upgrades, but it is a trusted legacy
mode: it has global visibility and does not enforce organization or requester separation. Use
version 2 for shared or production deployments.

## Trust boundary

The auth file, service process, runtime policy, and database administrator remain trusted.
Organization checks do not protect against code with direct database access or code that can call
the low-level trusted `store.decide` method. Keep the database and auth file outside model-writable
paths and restrict the CLI to trusted operators. Static bearer tokens are not an identity provider;
rotate them through a secret manager and terminate TLS at a trusted private ingress.

For high-risk actions, use a service subject for `requested_by`, a human subject for review, short
approval expiry, downstream business idempotency, atomic value limits, and audit export to a
separately protected system.
