# PostgreSQL runtime storage

> PostgreSQL support is available on the main branch and is not part of the current PyPI release.

Use PostgreSQL when multiple AgentBarrier processes, gateways, API instances, or workers need one
durable approval and execution boundary. It preserves the same request binding, single execution
claim, replay, pause, limit, and integrity-linked receipt behavior as the SQLite backend.

## Install

```bash
python -m pip install 'agentbarrier[postgres] @ git+https://github.com/binaydhakal/agentbarrier.git'
```

AgentBarrier depends on the Psycopg interface. A deployment may choose the system, binary, or pool
installation appropriate for its platform. Keep that choice in the application's lock file.

## Provision and migrate

Put the connection string in a secret manager or environment variable. The CLI accepts only the
environment variable's name, so the DSN does not appear in process arguments or shell history:

```bash
export AGENTBARRIER_DATABASE_URL='postgresql://agentbarrier_migrator@db/agentbarrier'

agentbarrier database migrate \
  --postgres-dsn-env AGENTBARRIER_DATABASE_URL \
  --postgres-schema agentbarrier \
  --postgres-create-schema
```

`--postgres-create-schema` is an explicit first-deployment operation. Omit it after the schema has
been provisioned. Normal commands never create a missing schema and never run migrations; they
fail closed until `database migrate` has established the current schema version.

The schema name must be a lowercase, unquoted PostgreSQL identifier. Use a dedicated schema rather
than `public`. The migration identity needs permission to create that schema on first deployment
and to create or alter objects within it on upgrades. The runtime identity needs `USAGE` on the
schema; `SELECT`, `INSERT`, `UPDATE`, and `DELETE` on its tables; and `USAGE` and `SELECT` on its
sequences. Exact role creation and authentication should follow the database platform's identity
and secret-management controls.

After every migration, grant the runtime identity access to newly created objects. PostgreSQL
default privileges can automate this when they are configured for the migration owner:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE agentbarrier_migrator IN SCHEMA agentbarrier
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentbarrier_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE agentbarrier_migrator IN SCHEMA agentbarrier
  GRANT USAGE, SELECT ON SEQUENCES TO agentbarrier_runtime;
GRANT USAGE ON SCHEMA agentbarrier TO agentbarrier_runtime;
```

Use real role names from the deployment. Do not grant the agent or model access to either database
identity.

## Run services

Change the environment variable to a least-privilege runtime DSN, then select the same backend for
any operational command:

```bash
export AGENTBARRIER_DATABASE_URL='postgresql://agentbarrier_runtime@db/agentbarrier'

agentbarrier database status \
  --postgres-dsn-env AGENTBARRIER_DATABASE_URL \
  --postgres-schema agentbarrier

agentbarrier dashboard \
  --postgres-dsn-env AGENTBARRIER_DATABASE_URL \
  --postgres-schema agentbarrier \
  --auth-config approval-auth.json
```

The same `--postgres-dsn-env` and `--postgres-schema` options work with approvals, controls, the
approval API, MCP gateways, and the webhook worker. Configure exactly one backend: `--db` for
SQLite or `--postgres-dsn-env` for PostgreSQL.

Python applications can use the backend directly:

```python
from agentbarrier.runtime import PostgresRuntimeStore, RuntimeBarrier

with PostgresRuntimeStore(dsn, schema="agentbarrier") as store:
    barrier = RuntimeBarrier(policy=policy, store=store, namespace="support-agent")
```

Provisioning code can opt in to `migrate=True` and, only on the first deployment,
`create_schema=True`. Application startup should retain the default validation-only behavior.

## Concurrency model

Each state-changing transaction takes a transaction-level PostgreSQL advisory lock derived from
the dedicated schema. This serializes approval transitions, execution claims, limit reservations,
and global receipt-chain insertion across processes. It favors simple safety invariants over write
throughput; independent schemas do not share the lock.

The default lock wait is 30 seconds and can be changed through the Python API. A timeout or storage
failure prevents the consequential function from being called. Monitor lock waits and transaction
latency before raising traffic limits. AgentBarrier does not use a connection pool in this release;
applications should keep a store open for the lifetime of each service process rather than opening
one for every action.

## Backup, restore, and upgrades

The SQLite `database backup` command does not apply to PostgreSQL. Use managed snapshots or
`pg_dump` with credentials that can read the dedicated schema. Back up before upgrading, protect
the backup as sensitive application data, and test restoration in an isolated database.

A safe upgrade sequence is:

1. stop AgentBarrier writers or place the surrounding system in maintenance mode;
2. take and verify a database backup;
3. run `agentbarrier database migrate` once with the migration identity;
4. run `agentbarrier database status` with the runtime identity;
5. verify both receipt chains and exercise one synthetic approval lifecycle; and
6. restart writers gradually while monitoring unknown outcomes and lock latency.

Do not point an older AgentBarrier version at a newer schema. Roll back application code only after
restoring a compatible backup. Receipt hashes detect accidental edits; they are not signatures and
do not protect against an identity that can rewrite all runtime rows.

The [production deployment and recovery guide](deployment.md) adds a complete restore drill,
maintenance-window upgrade sequence, rollback boundary, readiness checks, and incident runbook.

## CI contract

Project CI starts a real PostgreSQL service, runs the shared SQLite/PostgreSQL behavioral contract,
tests migrations and service decisions, and installs the built wheel into a clean environment for
an approval → execution → replay audit. The audit also proves that pauses and action limits are
enforced by the PostgreSQL execution-claim transaction.
