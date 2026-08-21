# Production deployment and recovery

This guide describes a production baseline for AgentBarrier's approval control plane. The checked-in
container and Compose files are a reviewable single-host reference, not a managed platform. Adapt
them to your orchestrator, identity provider, secret manager, TLS ingress, monitoring, and database
service.

## Deployment boundary

A complete deployment has five security boundaries:

1. the agent application or MCP gateway wraps every consequential tool and cannot reach the
   downstream credential through another route;
2. PostgreSQL is the authoritative action, control, and receipt store for shared deployments;
3. the approval API or dashboard is reachable only through trusted TLS ingress and authenticates a
   distinct reviewer identity;
4. migration credentials are separate from runtime credentials and are unavailable to live agent
   processes; and
5. backups, logs, metrics, webhook state, and Slack state are protected as sensitive operational
   data.

AgentBarrier is not a sandbox. A process that can call the original function, reach the upstream
MCP server, use the downstream credential, or rewrite the runtime database can bypass the control
plane.

## Image and Compose baseline

The reference [`Dockerfile`](../deploy/Dockerfile) installs dependencies from `uv.lock`, including
the self-contained `postgres-binary` extra, runs as UID/GID 10001, and contains no compiler in the
runtime stage. The
[`compose.yml`](../deploy/compose.yml) demonstrates:

- PostgreSQL on an internal-only database network without a host-published port;
- an explicit one-shot schema migration before the API starts;
- a read-only API container with every Linux capability dropped and `no-new-privileges` enabled;
- a separate API ingress network, loopback-only host port, and public readiness check; and
- health-based database ordering plus successful-migration ordering.

Docker Compose supports `service_healthy` and `service_completed_successfully` dependency
conditions as used here; see Docker's official
[startup-order documentation](https://docs.docker.com/compose/how-tos/startup-order/).

For a local review of the production topology:

```bash
cp deploy/.env.example deploy/.env
cp deploy/config/approval-auth.example.json deploy/config/approval-auth.json
chmod 600 deploy/.env deploy/config/approval-auth.json
```

Generate independent random migration, runtime, and reviewer credentials. Put the two PostgreSQL
values and matching percent-encoded DSNs in `deploy/.env`. The initialization script creates a
least-privilege runtime role and default table/sequence grants; it runs only when PostgreSQL creates
a fresh data directory. For an existing database, provision equivalent roles explicitly.

Have the secret manager inject the reviewer token as `AGENTBARRIER_REVIEWER_TOKEN`, then generate
the digest without putting the plaintext token in shell history:

```bash
agentbarrier auth hash-token --token-env AGENTBARRIER_REVIEWER_TOKEN
```

Replace the placeholder digest, organization, namespace, subject, and roles in the copied auth
file. Then validate and start:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yml build --pull
docker compose --env-file deploy/.env -f deploy/compose.yml up -d
curl --fail http://127.0.0.1:8787/health/ready
```

The reference publishes only the API to host loopback. Put a private TLS proxy or service mesh in
front of it before remote access. Do not expose PostgreSQL or the upstream MCP service to agent
networks. For a real deployment, build once from the audited release tag, record the source commit
and image digest, scan the image, sign it, and deploy that immutable digest rather than rebuilding
independently on each host. Pin the base and `uv` images by digest in your maintained derivative.

## Roles and secrets

Use at least these independent identities:

| Identity | Minimum access |
| --- | --- |
| migration job | Create or alter only the dedicated AgentBarrier schema. |
| protected runtime/MCP gateway | Read and change runtime rows; no schema ownership. |
| approval service | Read actions and write authorized decisions; no schema ownership. |
| backup job | Database backup or snapshot permission; no application decision credential. |
| reviewer | Version 2 API/dashboard role scoped to one organization and its namespaces. |

The Compose file uses environment substitution only to remain portable. Container environments are
readable to sufficiently privileged host operators. Production orchestration should inject the
complete DSN from its secret manager, rotate it on a tested schedule, and prevent it from reaching
the model, tool arguments, logs, image layers, or source control. The authorization file contains
token digests but is still credential material.

## Readiness, health, and alerts

`GET /health/ready` proves that the process can validate the expected runtime schema. It does not
prove that a downstream payment, database, email, or deployment provider is healthy. Remove an
instance from traffic when readiness fails; do not automatically retry an action whose execution
already started.

Alert on:

- any `unknown` outcome;
- invalid action or control receipt chains;
- repeated action-binding failures, policy denials, limit blocks, or emergency pauses;
- database connection, transaction-lock, or migration failures;
- webhook or Slack dead letters;
- approval latency and unusually high pending counts; and
- missing telemetry from an otherwise healthy process.

Use the [observability guide](observability.md) for privacy-safe traces, metrics, logs, and suggested
alerts. Runtime receipts—not telemetry—are the authoritative lifecycle record.

## SQLite backup and restore drill

SQLite is suitable for one trusted host. Stop writers for a restore, but the backup command itself
uses SQLite's consistent online backup operation:

```bash
agentbarrier audit --db /srv/agentbarrier/runtime.db
agentbarrier controls status --db /srv/agentbarrier/runtime.db --json
agentbarrier database backup \
  --db /srv/agentbarrier/runtime.db \
  --output /secure-backups/runtime-2026-08-21.db
```

The command refuses to overwrite a file, verifies the completed database, and uses mode `0600`.
Encrypt and copy the backup off-host. To test recovery, restore to an isolated path, run `database
status`, verify both receipt chains, and replay a completed action while confirming the downstream
effect is not called again. Only then consider the backup usable.

For an actual restore, pause or stop every writer, preserve the failed database and its WAL/SHM
files for investigation, place the verified backup at the configured path with the correct owner,
run the same checks, and start one instance before restoring traffic.

## PostgreSQL backup and restore drill

For small installations, take a custom-format logical backup from a client version at least as new
as the server:

```bash
pg_dump --format=custom --file=agentbarrier.dump "$AGENTBARRIER_POSTGRES_DSN"
pg_restore --list agentbarrier.dump >/dev/null
```

`pg_dump` makes a consistent export while the database remains in use, and custom format is restored
with `pg_restore`; see the official PostgreSQL
[`pg_dump` reference](https://www.postgresql.org/docs/18/app-pgdump.html) and
[backup chapter](https://www.postgresql.org/docs/18/backup.html). Managed production databases
should normally combine encrypted automated snapshots with point-in-time recovery rather than rely
only on an application-level logical dump.

At least quarterly, restore into a new isolated database and schema, start the exact matching
AgentBarrier image against it, and verify:

1. schema version and backend marker;
2. action and control receipt chains;
3. counts by lifecycle status, especially `executing` and `unknown`;
4. a known completed action replays without calling its downstream operation; and
5. version 2 organization filtering and requester/reviewer separation still fail closed.

Never restore over a live database. Treat a dump as untrusted executable database input if its
source administrators are not trusted; PostgreSQL warns that restore can execute source-controlled
code.

## Upgrade and rollback

AgentBarrier schema changes are deliberately explicit and fail closed. Current releases do not
support mixed application versions against one upgraded schema, so use a short maintenance window
instead of pretending an unsafe zero-downtime rolling upgrade is possible.

1. Activate an emergency pause and stop new agent/MCP traffic.
2. Drain workers. Investigate every action still marked `executing`; after lease expiry it becomes
   `unknown`, not retryable.
3. Verify both receipt chains and take a tested backup or snapshot.
4. Run the target image and application tests against a restored copy.
5. Stop all old API, dashboard, gateway, Slack, webhook, and application processes.
6. Run exactly one target-version migration job using the migration identity.
7. Start target-version services, require readiness, verify authorization isolation, and exercise a
   synthetic pending → approved → succeeded → replayed flow.
8. Restore traffic gradually, then clear the pause with an audited reason.

If migration or validation fails, keep traffic paused. Do not start old code against the newer
schema. Roll back by restoring the pre-upgrade snapshot into a new database, point the complete old
deployment at it, validate it, and only then restore traffic. Preserve the failed upgraded database
for diagnosis. Any real effects committed after the backup must be reconciled before rollback to
avoid duplicate execution.

## Incident runbook

When control integrity or a downstream provider is in doubt:

1. pause globally or at the narrowest safe namespace/tool scope;
2. remove the affected gateway or application from traffic;
3. preserve runtime, webhook, and Slack databases plus relevant logs and configuration versions;
4. verify receipt chains and enumerate `executing` and `unknown` actions;
5. check the authoritative downstream system by business idempotency key;
6. reconcile only with positive commit evidence or reliable absence evidence;
7. rotate exposed database, bearer, Slack, webhook, and upstream credentials;
8. repair complete mediation and add a model-free regression test; and
9. resume gradually with an audited reason and heightened alerts.

If an attacker could write the database, receipt hashes alone cannot establish integrity because
the chain can be recomputed. Use independent database audit logs, immutable backup history, webhook
or SIEM copies, and infrastructure evidence.

## Recovery objectives

Choose and test an explicit recovery point objective (RPO) and recovery time objective (RTO). The
correct values depend on action volume and consequence. Backup frequency must cover both the
runtime database and auxiliary Slack/webhook state. A backup that has never been restored and
verified does not satisfy the objective. Record drill dates, artifact digests, restore duration,
row counts, receipt verification, and the operator who approved the result.
