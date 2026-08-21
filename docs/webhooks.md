# Signed runtime webhooks

> Signed webhooks are part of AgentBarrier's stable 1.x package. Test each receiver, redaction
> policy, and retry configuration before using it for production decisions or alerts.

The webhook worker turns AgentBarrier's integrity-linked runtime receipts into durable outbound
events. It can notify an approval service when an action needs review, update an operations system
after a decision, and alert an incident workflow when execution has an unknown outcome.

The worker is separate from the tool process and approval API:

```text
Protected Python or MCP tool  ->  runtime database  ->  webhook worker
                                                        |
                                                        v
                              approval UI, queue, SIEM, or automation receiver
```

The runtime transaction never waits for the remote receiver. The worker first stores a canonical,
redacted event in its own SQLite outbox and then delivers it with a byte-exact HMAC signature.

## Install

```bash
python -m pip install 'agentbarrier[service]'
```

## Configure endpoints

Generate a random signing secret with at least 256 bits of entropy and provide it through an
environment variable. Never place the plaintext secret in the JSON file.

Create `webhooks.json` outside model-writable directories:

```json
{
  "version": "1",
  "endpoints": [
    {
      "id": "operations",
      "url": "https://hooks.example.com/agentbarrier",
      "secret_env": "AGENTBARRIER_OPERATIONS_WEBHOOK_SECRET",
      "events": [
        "approval_requested",
        "approved",
        "rejected",
        "execution_unknown",
        "execution_abandoned"
      ],
      "redact_argument_paths": ["customer.ssn", "payment.card_number"],
      "timeout_seconds": 10,
      "max_attempts": 5,
      "initial_backoff_seconds": 1,
      "max_backoff_seconds": 60,
      "start_from": "latest"
    }
  ]
}
```

The config is strict and rejects unknown fields, invalid event names, duplicate endpoints or
events, unsafe URLs, weak secrets, and invalid retry values. Non-loopback endpoints require HTTPS;
redirects are never followed. URLs cannot contain credentials, query strings, or fragments. HTTP
timeouts are capped at 30 seconds and must remain shorter than the exclusive delivery-claim lease.

`start_from` controls the first worker run:

- `latest` begins after the newest existing receipt and delivers only future events. This is the
  safer default when adding notifications to a live database.
- `beginning` creates deliveries for matching historical receipts as well as future events.

Supported events are `policy_allowed`, `policy_denied`, `approval_requested`, `approved`,
`rejected`, `expired`, `execution_started`, `execution_succeeded`, `execution_unknown`,
`execution_abandoned`, `emergency_pause_blocked`, `limit_blocked`,
`reconciliation_committed`, `reconciliation_not_committed`, and
`result_replayed`.

An endpoint ID binds its URL, filters, redaction, retry policy, and starting mode to durable state.
Use a new ID when those semantics change. The secret value is deliberately excluded from that
binding so it can be rotated without discarding delivery history.

## Run the worker

Run continuously next to the runtime database:

```bash
agentbarrier webhooks run \
  --db agentbarrier.db \
  --state-db agentbarrier-webhooks.db \
  --config webhooks.json
```

For a scheduler, job runner, or deployment check, process all work that is currently due once:

```bash
agentbarrier webhooks run \
  --db agentbarrier.db \
  --state-db agentbarrier-webhooks.db \
  --config webhooks.json \
  --once
```

The state database stores endpoint checkpoints, redacted request bodies, claims, retry counts, and
delivery outcomes. It never stores endpoint URLs or signing secrets. Keep it durable and writable
by only one service identity; multiple workers may share it because claims use SQLite transactions
and expiring leases.

## Delivery contract

Every request uses `POST` with `Content-Type: application/cloudevents+json` and these headers:

| Header | Meaning |
| --- | --- |
| `X-AgentBarrier-Event-Id` | Stable ID such as `runtime-receipt-42`. |
| `X-AgentBarrier-Timestamp` | Unix time in seconds for this attempt. |
| `X-AgentBarrier-Signature` | `v1=` followed by an HMAC-SHA256 hex digest. |

The signature input is the ASCII timestamp, one period byte, and the exact HTTP body bytes:

```text
HMAC-SHA256(secret, timestamp + "." + body)
```

The body follows the CloudEvents 1.0 shape and includes the action ID, namespace, tool name,
redacted arguments, request digest, policy identity, current event status, and the exact audit
receipt. It deliberately excludes the business idempotency key and stored execution result.

Keys commonly used for secrets—including `api_key`, `apiKey`, `accessToken`, `password`,
`authorization`, `cookie`, `privateKey`, and `clientSecret`—are automatically redacted at any
depth. Add application-specific dotted paths with `redact_argument_paths`. Redaction is a safety
layer, not a reason to place credentials in tool arguments.

## Verify a request

The receiver must verify the signature against the raw body before decoding JSON. In Python:

```python
import hashlib
import hmac
import time


def verify_webhook(*, body: bytes, headers: dict[str, str], secret: str) -> bool:
    timestamp = headers["X-AgentBarrier-Timestamp"]
    if abs(time.time() - int(timestamp)) > 300:
        return False
    signed = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("ascii"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers["X-AgentBarrier-Signature"], f"v1={digest}")
```

After verification, atomically record `X-AgentBarrier-Event-Id` before applying receiver-side
effects. Reject stale timestamps and malformed headers. During secret rotation, allow both the old
and new secret for a short overlap, then remove the old one.

## Retries and duplicate handling

A 2xx response marks a delivery complete. Network errors and every non-2xx status are retried with
bounded exponential backoff. A process crash leaves an expiring claim; another worker recovers it
after the lease. Exhausted deliveries become `dead` and are not retried automatically.

This is an at-least-once protocol. If the receiver accepts a request but its response is lost, the
same event may arrive again. The event ID and body remain stable across retries, while the timestamp
and signature are regenerated. Receivers must therefore deduplicate by event ID.

Inspect delivery state without exposing bodies, URLs, or secrets:

```bash
agentbarrier webhooks status --state-db agentbarrier-webhooks.db
agentbarrier webhooks status --state-db agentbarrier-webhooks.db --json
```

After fixing a receiver outage, explicitly requeue one exact dead delivery:

```bash
agentbarrier webhooks retry runtime-receipt-42 \
  --endpoint operations \
  --state-db agentbarrier-webhooks.db
```

Manual retry resets only that delivery's bounded attempt counter. Pending, in-flight, and delivered
events cannot be requeued with this command.

## Deployment checklist

1. Keep the runtime database, webhook state database, config, and signing secret outside
   model-writable paths.
2. Use HTTPS and a private or allow-listed receiver; the configured URL is a trusted operator
   input with network access from the worker.
3. Verify the HMAC over raw bytes, enforce a short timestamp window, and deduplicate event IDs
   before applying effects.
4. Select only required events and configure every application-specific sensitive argument path.
5. Alert on `dead` deliveries, repeated retries, `execution_unknown`, and
   `execution_abandoned` events.
6. Back up the runtime and webhook state databases consistently and test restore procedures.
7. Keep approval authority in the authenticated API or another identity-bound service; receipt of
   a webhook is notification, not authorization to mutate AgentBarrier state directly.

See the [approval API guide](approval-api.md) for identity-bound decisions and the
[threat model](threat-model.md) for the complete trust boundary.

The runtime source may be PostgreSQL by replacing `--db` with `--postgres-dsn-env NAME` and
optionally `--postgres-schema NAME`. Webhook delivery state remains a separate SQLite outbox in
this release. Provision the shared runtime schema using the [PostgreSQL guide](postgresql.md).
