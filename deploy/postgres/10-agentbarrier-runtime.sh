#!/bin/sh
set -eu

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=migrator_user="$POSTGRES_USER" \
  --set=runtime_user="$AGENTBARRIER_RUNTIME_DB_USER" \
  --set=runtime_password="$AGENTBARRIER_RUNTIME_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'runtime_user') \gexec

SELECT format(
  'CREATE SCHEMA IF NOT EXISTS agentbarrier AUTHORIZATION %I',
  :'migrator_user'
) \gexec

SELECT format('GRANT USAGE ON SCHEMA agentbarrier TO %I', :'runtime_user') \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA agentbarrier '
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'migrator_user',
  :'runtime_user'
) \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA agentbarrier '
  'GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'migrator_user',
  :'runtime_user'
) \gexec
SQL
