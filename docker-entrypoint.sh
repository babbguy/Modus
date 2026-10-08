#!/bin/sh
# =============================================================================
# Modus Orchestrator — container entrypoint
# =============================================================================
# Applies Alembic schema migrations for PostgreSQL deployments before starting
# the application, then exec's the container CMD (uvicorn).
#
# Why this exists:
#   - PostgreSQL deployments have NO auto-schema step. init_db() only runs
#     Base.metadata.create_all for SQLite (see orchestrator/db/session.py).
#     Without this entrypoint, a fresh Postgres deploy starts against an empty
#     database and every query fails.
#   - SQLite deployments are left untouched: init_db() creates all tables via
#     create_all on startup, so running Alembic there is unnecessary.
#
# POSIX sh only — no bashisms. Runs as the non-root `modus` user set by the
# Dockerfile (USER modus). Alembic needs only network access to the DB, no
# elevated privileges.
# =============================================================================

set -eu

# Escape hatch for orchestrated deployments (e.g. the Helm chart) that run
# migrations in a dedicated step: multiple replicas exec-ing this entrypoint
# would otherwise race `alembic upgrade` against the same database.
if [ "${MODUS_SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] MODUS_SKIP_MIGRATIONS=1 — skipping Alembic (handled externally)."
    exec "$@"
fi

# Resolve the configured DSN. Mirrors orchestrator/core/config.py:
#   env prefix MODUS_ + `database_url`, default = SQLite single-file.
DB_URL="${MODUS_DATABASE_URL:-sqlite+aiosqlite:///modus.db}"

# Lowercase for a case-insensitive scheme check (matches config.is_sqlite,
# which tests `"sqlite" in database_url.lower()`).
DB_URL_LC="$(printf '%s' "$DB_URL" | tr '[:upper:]' '[:lower:]')"

case "$DB_URL_LC" in
    *sqlite*)
        # SQLite: init_db() handles table creation via create_all. Nothing to do.
        echo "[entrypoint] SQLite DSN detected — skipping Alembic (tables created by init_db)."
        ;;
    *)
        # PostgreSQL (or any non-SQLite DSN, which config only allows to be
        # Postgres): apply migrations. 'heads' also works should the history
        # ever branch.
        echo "[entrypoint] PostgreSQL DSN detected — applying Alembic migrations (alembic upgrade heads)."
        alembic upgrade heads
        ;;
esac

exec "$@"
