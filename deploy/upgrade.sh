#!/usr/bin/env bash
# =============================================================================
# Modus Upgrade Script
# =============================================================================
# Updates the source checkout (or pulls published images), backs up the
# database, restarts containers, verifies health, and rolls back automatically
# if the health check fails. Run it from the deploy/ directory of a checkout.
#
# Usage:
#   ./upgrade.sh                  # Upgrade to latest
#   ./upgrade.sh --version 1.0.0  # Select a specific image tag (MODUS_VERSION)
#   ./upgrade.sh --dry-run        # Show what would happen
#
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
# =============================================================================

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=""
DRY_RUN=false
HEALTH_TIMEOUT=90
ROLLBACK_IMAGE=""

# Colors
if [ -t 1 ]; then
    RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m'
    BOLD='\033[1m' NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BOLD='' NC=''
fi

log()   { printf "${GREEN}[upgrade]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[upgrade]${NC} %s\n" "$*"; }
error() { printf "${RED}[upgrade]${NC} %s\n" "$*" >&2; }
fatal() { error "$@"; exit 1; }

# ── Parse Arguments ──────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--version VERSION] [--dry-run]"
            exit 0
            ;;
        *) fatal "Unknown option: $1" ;;
    esac
done

# ── Detect Compose Command ──────────────────────────────────────────────────

COMPOSE_CMD=""
if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    fatal "Docker Compose not found."
fi

cd "${INSTALL_DIR}"

if [ -f docker-compose.yml ]; then
    COMPOSE_FILE="docker-compose.yml"
elif [ -f docker-compose.prod.yml ]; then
    COMPOSE_FILE="docker-compose.prod.yml"
else
    fatal "No docker-compose.yml found in ${INSTALL_DIR}. Is this a Modus installation?"
fi
export COMPOSE_FILE

# ── Record Current State ────────────────────────────────────────────────────

log "Checking current installation..."

CURRENT_IMAGE=$(docker inspect modus-orchestrator --format='{{.Config.Image}}' 2>/dev/null || echo "unknown")
log "  Current image: ${CURRENT_IMAGE}"

# ── Set Target Version ──────────────────────────────────────────────────────

if [ -n "${VERSION}" ]; then
    # Update .env with new version
    if grep -q '^MODUS_VERSION=' .env 2>/dev/null; then
        sed -i.bak "s|^MODUS_VERSION=.*|MODUS_VERSION=${VERSION}|" .env
        rm -f .env.bak
    else
        echo "MODUS_VERSION=${VERSION}" >> .env
    fi
    log "  Target version: ${VERSION}"
else
    VERSION=$(grep '^MODUS_VERSION=' .env 2>/dev/null | cut -d= -f2 || echo "latest")
    VERSION="${VERSION:-latest}"
    log "  Target version: ${VERSION}"
fi

if [ "${DRY_RUN}" = true ]; then
    log "[DRY RUN] Would upgrade from ${CURRENT_IMAGE} to version ${VERSION}"
    log "[DRY RUN] Would back up database to ${INSTALL_DIR}/backups/"
    log "[DRY RUN] Would pull new images, restart, and verify health"
    exit 0
fi

# ── Backup Database ─────────────────────────────────────────────────────────

log "Backing up database..."

BACKUP_DIR="${INSTALL_DIR}/backups"
BACKUP_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "${BACKUP_DIR}"

# Detect the effective database DSN from the running orchestrator container.
# Compose merges env_file + environment, so the container env is authoritative;
# fall back to .env for a stopped installation.
DB_URL=$(docker inspect modus-orchestrator \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep '^MODUS_DATABASE_URL=' | head -1 | cut -d= -f2- || echo "")
if [ -z "${DB_URL}" ]; then
    DB_URL=$(grep '^MODUS_DATABASE_URL=' .env 2>/dev/null | cut -d= -f2- || echo "")
fi

IS_POSTGRES=false
case "$(printf '%s' "${DB_URL}" | tr '[:upper:]' '[:lower:]')" in
    *postgres*) IS_POSTGRES=true ;;
esac

VOLUME_PATH=""
PG_URL=""
BACKUP_FILE=""

if [ "${IS_POSTGRES}" = true ]; then
    # PostgreSQL: logical dump via pg_dump. Run in a throwaway postgres client
    # container sharing the orchestrator's network namespace, so it reaches the
    # same database host (a compose service or an external managed DB).
    # --clean --if-exists makes the plain-SQL dump self-contained for restore.
    BACKUP_FILE="${BACKUP_DIR}/modus-${BACKUP_TIMESTAMP}.sql"
    # SQLAlchemy DSN → libpq URL (pg_dump does not understand +asyncpg/+psycopg2).
    PG_URL=$(printf '%s' "${DB_URL}" | sed 's|+asyncpg||; s|+psycopg2||')
    if docker run --rm --network "container:modus-orchestrator" \
            postgres:16-alpine \
            pg_dump --no-owner --no-privileges --clean --if-exists "${PG_URL}" \
            > "${BACKUP_FILE}" 2>/dev/null; then
        log "  Database backed up to ${BACKUP_FILE} (pg_dump)"
    else
        rm -f "${BACKUP_FILE}"
        BACKUP_FILE=""
        warn "  Could not back up PostgreSQL database (check DB connectivity)."
    fi
    # Prune old backups (keep last 10)
    ls -t "${BACKUP_DIR}"/modus-*.sql 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
else
    # SQLite: copy the single database file out of the Docker volume.
    BACKUP_FILE="${BACKUP_DIR}/modus-${BACKUP_TIMESTAMP}.db"
    VOLUME_PATH=$(docker volume inspect modus_data --format='{{.Mountpoint}}' 2>/dev/null \
        || docker volume inspect "${INSTALL_DIR##*/}_modus_data" --format='{{.Mountpoint}}' 2>/dev/null \
        || echo "")

    if [ -n "${VOLUME_PATH}" ] && [ -f "${VOLUME_PATH}/modus.db" ]; then
        cp "${VOLUME_PATH}/modus.db" "${BACKUP_FILE}"
        log "  Database backed up to ${BACKUP_FILE}"
    else
        # Fallback: copy via container
        docker cp modus-orchestrator:/app/data/modus.db "${BACKUP_FILE}" 2>/dev/null \
            && log "  Database backed up to ${BACKUP_FILE}" \
            || { warn "  Could not back up database (new installation or empty DB)"; BACKUP_FILE=""; }
    fi
    # Prune old backups (keep last 10)
    ls -t "${BACKUP_DIR}"/modus-*.db 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
fi

# ── Pull New Images ──────────────────────────────────────────────────────────

if [ -f "${INSTALL_DIR}/../Dockerfile.orchestrator" ]; then
    # Source checkout: update the code (if it is a git clone) and rebuild.
    log "Source checkout detected - updating and rebuilding images..."
    if git -C "${INSTALL_DIR}" rev-parse --is-inside-work-tree &>/dev/null; then
        git -C "${INSTALL_DIR}" pull --ff-only || warn "  git pull failed; rebuilding from the current checkout."
    fi
    ${COMPOSE_CMD} build
else
    log "Pulling new images..."
    ${COMPOSE_CMD} pull --quiet 2>/dev/null || ${COMPOSE_CMD} pull
fi

# ── Run Database Migrations ─────────────────────────────────────────────────

log "Running database migrations..."
# SQLite builds its schema via create_all + the additive-ALTER path on startup
# and never stamps alembic_version, so `alembic upgrade heads` would fail
# ("table already exists") and abort the upgrade under `set -euo pipefail`.
# Only run Alembic for non-SQLite (PostgreSQL) DSNs — mirrors the DSN detection
# in docker-entrypoint.sh. Empty/undetected DSN defaults to SQLite (the app's
# default), so skip there too.
case "$(printf '%s' "${DB_URL}" | tr '[:upper:]' '[:lower:]')" in
    ""|*sqlite*)
        log "  SQLite DSN detected — skipping Alembic (schema handled by init_db)."
        ;;
    *)
        # Service name is 'orchestrator' (modus-orchestrator is only the container_name).
        ${COMPOSE_CMD} -f "$COMPOSE_FILE" run --rm orchestrator alembic upgrade heads
        ;;
esac

# ── Restart Containers ───────────────────────────────────────────────────────

log "Restarting Modus..."

# Detect if org-wide mode is active
if docker ps --format '{{.Names}}' | grep -q modus-conductor; then
    ${COMPOSE_CMD} --profile org-wide up -d
else
    ${COMPOSE_CMD} up -d
fi

# ── Verify Health ────────────────────────────────────────────────────────────

log "Waiting for health check..."

# Read port from .env if available, fallback to 8080
HEALTH_PORT=$(grep '^MODUS_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "8080")
HEALTH_PORT="${HEALTH_PORT:-8080}"

HEALTH_ELAPSED=0
HEALTHY=false

while [ ${HEALTH_ELAPSED} -lt ${HEALTH_TIMEOUT} ]; do
    if curl -sf "http://localhost:${HEALTH_PORT}/health" &>/dev/null; then
        HEALTHY=true
        break
    fi
    sleep 2
    HEALTH_ELAPSED=$((HEALTH_ELAPSED + 2))
done

# ── Rollback on Failure ─────────────────────────────────────────────────────

if [ "${HEALTHY}" = false ]; then
    error "Health check failed after ${HEALTH_TIMEOUT}s. Rolling back..."

    # Restore database backup
    if [ -n "${BACKUP_FILE}" ] && [ -f "${BACKUP_FILE}" ]; then
        if [ "${IS_POSTGRES}" = true ]; then
            # Replay the (--clean --if-exists) plain-SQL dump via psql.
            docker run --rm --network "container:modus-orchestrator" \
                -i postgres:16-alpine psql --quiet "${PG_URL}" < "${BACKUP_FILE}" >/dev/null 2>&1 \
                && log "  Database restored from backup (psql)" \
                || warn "  Automatic PostgreSQL restore failed. Manual restore: psql <dsn> < ${BACKUP_FILE}"
        elif [ -n "${VOLUME_PATH}" ]; then
            cp "${BACKUP_FILE}" "${VOLUME_PATH}/modus.db"
            log "  Database restored from backup"
        else
            docker cp "${BACKUP_FILE}" modus-orchestrator:/app/data/modus.db 2>/dev/null || true
            log "  Database restored from backup"
        fi
    fi

    # Revert to previous image if known
    if [ "${CURRENT_IMAGE}" != "unknown" ] && [ -n "${CURRENT_IMAGE}" ]; then
        if grep -q '^MODUS_VERSION=' .env; then
            PREV_VERSION=$(echo "${CURRENT_IMAGE}" | sed 's/.*://')
            sed -i.bak "s|^MODUS_VERSION=.*|MODUS_VERSION=${PREV_VERSION}|" .env
            rm -f .env.bak
        fi
        ${COMPOSE_CMD} pull --quiet 2>/dev/null || true
        ${COMPOSE_CMD} up -d
        log "  Rolled back to ${CURRENT_IMAGE}"
    fi

    fatal "Upgrade failed. Previous version restored. Check logs: ${COMPOSE_CMD} logs"
fi

# ── Success ──────────────────────────────────────────────────────────────────

NEW_IMAGE=$(docker inspect modus-orchestrator --format='{{.Config.Image}}' 2>/dev/null || echo "unknown")
printf "\n${GREEN}${BOLD}Upgrade complete!${NC}\n"
log "  Previous: ${CURRENT_IMAGE}"
log "  Current:  ${NEW_IMAGE}"
log "  Backup:   ${BACKUP_FILE}"
printf "\n"
