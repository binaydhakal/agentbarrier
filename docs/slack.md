# Slack approvals

> Slack support is part of AgentBarrier's stable 1.x package. Use a private channel, a dedicated
> app, and the strict workspace, channel, and member allowlists described below.

AgentBarrier can post pending runtime actions to one private Slack channel and accept an approve or
reject decision from explicitly configured workspace members. Slack is a human interface only: the
runtime database remains the source of truth, and the protected action still passes through the
same exact-request, policy, approval, limit, pause, execution-claim, and replay boundary.

```text
Protected Python or MCP action
            |
            v
   AgentBarrier runtime database
            |
            v
 durable Slack worker -> private Slack channel -> signed button click
            ^                                      |
            +------------ exact binding -----------+
```

## Install

```bash
python -m pip install 'agentbarrier[slack]'
```

## Create the Slack app

Create an app for the intended workspace using Slack's app-management interface:

1. Add the bot token scope `chat:write`.
2. Install the app to the workspace and keep the generated `xoxb-...` bot token in a secret
   manager.
3. Invite the bot to a private channel dedicated to AgentBarrier approvals.
4. Enable Interactivity & Shortcuts and set the request URL to the public HTTPS address that maps
   to `/slack/interactions` on this service.

Slack documents [request signing](https://docs.slack.dev/authentication/verifying-requests-from-slack/),
[interactive request handling](https://docs.slack.dev/interactivity/handling-user-interaction/),
and the [`chat.postMessage` permission](https://docs.slack.dev/reference/methods/chat.postmessage/).
This integration uses HTTP interactivity; it does not currently implement Socket Mode.

## Configure exact reviewers

Create `slack.json` outside model-writable directories:

```json
{
  "version": "2",
  "workspace_id": "T01234567",
  "app_id": "A01234567",
  "channel_id": "C01234567",
  "organization_id": "acme",
  "namespaces": ["billing", "support"],
  "require_separate_approver": true,
  "bot_token_env": "AGENTBARRIER_SLACK_BOT_TOKEN",
  "signing_secret_env": "AGENTBARRIER_SLACK_SIGNING_SECRET",
  "reviewers": [
    {
      "user_id": "U01234567",
      "subject": "risk-team@example.com",
      "decisions": ["approve", "reject"]
    },
    {
      "user_id": "U07654321",
      "subject": "operations@example.com",
      "decisions": ["reject"]
    }
  ]
}
```

The IDs must be the exact Slack workspace, app, channel, and member IDs—not display names. The
`subject` is an operator-controlled audit label. A member may be restricted to approval, rejection,
or both. Unknown members receive a private denial message and cannot change runtime state.
Version 2 restricts notifications and decisions to the configured organization namespaces and can
forbid a Slack reviewer whose subject matches the action requester. The organization, namespaces,
and subjects should match the approval service's
[multi-user authorization](multi-user-authorization.md) configuration. Version 1 remains a legacy
global-store mode without tenant filtering or requester separation.

Load secrets into the named environment variables. AgentBarrier never accepts either secret as a
command-line argument and excludes both values from configuration representations and status
output:

```bash
export AGENTBARRIER_SLACK_BOT_TOKEN='xoxb-...'
export AGENTBARRIER_SLACK_SIGNING_SECRET='...'
```

## Run the service

Create or migrate the runtime database through the normal runtime workflow first. The Slack state
database is intentionally separate: it stores delivery attempts, exact posted-message bindings,
dead letters, and processed interaction signatures, but not bot tokens or action arguments.

```bash
agentbarrier slack serve \
  --db agentbarrier.db \
  --state-db agentbarrier-slack.db \
  --config slack.json
```

The listener defaults to `127.0.0.1:8789`. Put a trusted HTTPS reverse proxy in front of it and
configure Slack with the resulting URL, for example
`https://approvals.example.com/slack/interactions`. The proxy must preserve the untouched request
body and `X-Slack-Request-Timestamp` and `X-Slack-Signature` headers. Do not expose the runtime
database, Slack state database, dashboard, or upstream MCP server through this route.

For PostgreSQL runtime state, provision the schema first and replace `--db` with the name of the
environment variable that holds the DSN:

```bash
agentbarrier slack serve \
  --postgres-dsn-env AGENTBARRIER_POSTGRES_DSN \
  --postgres-schema agentbarrier \
  --state-db agentbarrier-slack.db \
  --config slack.json
```

The notification state remains SQLite in this release. Run one Slack service against a local disk
or a single host; do not place that file on an unreliable network filesystem or run independent
copies with unshared state.

## Decision and delivery guarantees

- Every button value contains the runtime action ID and the SHA-256 digest of the exact namespace,
  tool, canonical arguments, business idempotency key, and policy version.
- The interaction must match the configured app, workspace, channel, posted message timestamp,
  action ID, and request digest before reviewer authorization is considered.
- Slack's `v0` HMAC is verified over the untouched request body with constant-time comparison.
  Requests older than five minutes are rejected, and a processed signature is durably replay-safe.
- The audit identity is derived from the configured workspace, signed Slack member ID, and local
  subject. Button payloads cannot choose the reviewer identity or permission.
- Successful delivery records the exact Slack message timestamp. A click from a copied, forged, or
  different message fails closed.
- Slack messages use plain text blocks, disable link and media unfurling, and include accessible
  fallback text.
- If the complete exact action cannot fit within Slack's limits, AgentBarrier posts a notice with
  no decision buttons. Review it through the dashboard or CLI; Slack never approves truncated
  arguments.
- Notification attempts use crash-recoverable leases, bounded exponential backoff, Slack
  `Retry-After` guidance, and a terminal dead-letter state. Stable client message IDs reduce
  duplicate posts if a response is lost.
- Message updates happen after Slack is acknowledged. A failed cosmetic update never rolls back a
  durable runtime decision; the dashboard, CLI, API, and runtime receipts show the authoritative
  state.

Slack asks interactive endpoints to acknowledge within three seconds. AgentBarrier performs the
durable authorization and runtime decision before acknowledging, then updates the message in the
background. Keep database lock latency low. A timeout can cause Slack to retry, but the stored
signature and idempotent runtime transition prevent a second decision receipt.

## Operations

Inspect delivery state without loading Slack secrets:

```bash
agentbarrier slack status --state-db agentbarrier-slack.db
agentbarrier slack status --state-db agentbarrier-slack.db --json
```

After correcting a token, permission, channel, or network problem, grant one dead notification a
fresh bounded retry budget:

```bash
agentbarrier slack retry ACTION_ID --state-db agentbarrier-slack.db
```

Only `dead` notifications can be requeued. Posted and decided records keep their original exact
message binding.

## Production checklist

1. Use a private approval channel and grant access only to the people allowed to see exact action
   arguments and reviewer identities.
2. Put the bot token, signing secret, runtime credentials, config, and both state databases outside
   model-writable and web-served locations. Restrict file and process access.
3. Terminate TLS at trusted ingress, limit the interaction route to the expected body size, preserve
   the exact body, and rate limit abuse without rewriting valid Slack requests.
4. Rotate Slack credentials by updating the secret manager and restarting the one Slack service.
   Remove departed members from both the Slack workspace/channel and the AgentBarrier allowlist.
5. Alert on dead notifications, repeated unauthorized interactions, runtime `unknown` outcomes,
   invalid receipt chains, and unexpected service restarts.
6. Back up the runtime and Slack state consistently and test restoration. The Slack channel is not
   a backup or authoritative audit log.
7. Keep the dashboard or CLI available for oversized actions and for recovery when Slack is down.
8. Protect the downstream system with its own authorization and business-idempotency enforcement.

Slack compromise, workspace-admin compromise, or a stolen allowed member session can authorize the
decisions granted to that member. AgentBarrier verifies Slack's identity assertion and local
allowlist; it does not add phishing-resistant authentication or Slack
administration controls.
