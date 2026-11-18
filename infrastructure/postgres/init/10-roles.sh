#!/bin/sh
# Runs once, on first initialisation of the PostgreSQL data volume.
# claimos_migrator (POSTGRES_USER) owns schema objects and runs Alembic.
# claimos_app is the least-privileged runtime role: DML only, no DDL, no BYPASSRLS.
set -eu

# Production passes the password as a file (Docker secret): CLAIMOS_APP_DB_PASSWORD_FILE.
if [ -n "${CLAIMOS_APP_DB_PASSWORD_FILE:-}" ]; then
  CLAIMOS_APP_DB_PASSWORD="$(cat "$CLAIMOS_APP_DB_PASSWORD_FILE")"
fi
: "${CLAIMOS_APP_DB_PASSWORD:?CLAIMOS_APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 -v app_password="$CLAIMOS_APP_DB_PASSWORD" \
  --username "$POSTGRES_USER" --dbname postgres <<'SQL'
SELECT format(
  'CREATE ROLE claimos_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
  :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') \gexec

-- Dedicated database for integration tests.
SELECT 'CREATE DATABASE claimos_test OWNER claimos_migrator'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'claimos_test') \gexec
SQL

for db in claimos claimos_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE $db FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE $db TO claimos_app;
GRANT USAGE ON SCHEMA public TO claimos_app;
-- Tables created later by the migrator are usable by the app role. Append-only tables
-- revoke UPDATE/DELETE explicitly in their own migrations.
ALTER DEFAULT PRIVILEGES FOR ROLE claimos_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO claimos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE claimos_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO claimos_app;
SQL
done
