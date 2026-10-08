#!/usr/bin/env bash
# =============================================================================
# Modus Installer (from a source checkout)
# =============================================================================
# Builds and starts Modus with Docker Compose from the repository you cloned:
#
#   git clone https://github.com/babbguy/Modus.git
#   cd Modus/deploy
#   ./install.sh                # standalone: orchestrator + dashboard
#   ./install.sh --org-wide     # also start the Conductor
#
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
# =============================================================================

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ORG_WIDE=false

if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' NC=''
fi

log()   { printf "${GREEN}[modus]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[modus]${NC} %s\n" "$*"; }
error() { printf "${RED}[modus]${NC} %s\n" "$*" >&2; }
fatal() { error "$@"; exit 1; }

usage() {
    cat <<'USAGE'
Modus Installer

Run from the deploy/ directory of a cloned repository.

Options:
  --org-wide      Also start the Conductor (multi-app aggregation)
  --help          Show this help
USAGE
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --org-wide) ORG_WIDE=true; shift ;;
        --help|-h)  usage ;;
        *)          fatal "Unknown option: $1. Use --help for usage." ;;
    esac
done

# ── Prerequisites ────────────────────────────────────────────────────────────

log "Checking prerequisites..."

[ -f "${DEPLOY_DIR}/docker-compose.prod.yml" ] \
    || fatal "docker-compose.prod.yml not found next to this script. Run it from a cloned checkout."
[ -f "${DEPLOY_DIR}/../Dockerfile.orchestrator" ] \
    || fatal "Dockerfile.orchestrator not found. Run this script from the deploy/ directory of a full checkout."

command -v docker &>/dev/null \
    || fatal "Docker is required but not installed. See https://docs.docker.com/get-docker/"

COMPOSE_CMD=""
if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    fatal "Docker Compose is required but not installed. See https://docs.docker.com/compose/install/"
fi
log "  Compose: ${COMPOSE_CMD}"

cd "${DEPLOY_DIR}"
export COMPOSE_FILE=docker-compose.prod.yml

# ── Configure Environment ────────────────────────────────────────────────────

if [ ! -f .env ]; then
    cp .env.example .env
    log "Created deploy/.env from template"
fi

# Generate a master API key if the placeholder is still in place
if grep -q 'MODUS_MASTER_API_KEY=mds_master_CHANGE_ME' .env 2>/dev/null; then
    GENERATED_KEY="mds_master_$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
    sed -i.bak "s|MODUS_MASTER_API_KEY=mds_master_CHANGE_ME|MODUS_MASTER_API_KEY=${GENERATED_KEY}|" .env
    rm -f .env.bak
    log "Generated master API key (stored in deploy/.env)"
fi

if [ "${ORG_WIDE}" = true ]; then
    sed -i.bak 's|^MODUS_MODE=standalone|MODUS_MODE=org-wide|' .env
    rm -f .env.bak
    log "Configured for org-wide mode (Conductor enabled)"
fi

mkdir -p "${DEPLOY_DIR}/backups" "${DEPLOY_DIR}/plugins"

# ── Build and start ──────────────────────────────────────────────────────────

log "Building images from source (this may take a few minutes)..."
if [ "${ORG_WIDE}" = true ]; then
    ${COMPOSE_CMD} --profile org-wide up --build -d
else
    ${COMPOSE_CMD} up --build -d
fi

# ── Wait for health ──────────────────────────────────────────────────────────

PORT=$(grep '^MODUS_PORT=' .env 2>/dev/null | cut -d= -f2 || echo "8080")
PORT="${PORT:-8080}"

log "Waiting for Modus to become healthy..."
HEALTH_TIMEOUT=90
HEALTH_ELAPSED=0
while [ ${HEALTH_ELAPSED} -lt ${HEALTH_TIMEOUT} ]; do
    if curl -sf "http://localhost:${PORT}/health" &>/dev/null; then
        break
    fi
    sleep 2
    HEALTH_ELAPSED=$((HEALTH_ELAPSED + 2))
done

if [ ${HEALTH_ELAPSED} -ge ${HEALTH_TIMEOUT} ]; then
    warn "Modus did not become healthy within ${HEALTH_TIMEOUT}s."
    warn "Check logs: cd ${DEPLOY_DIR} && COMPOSE_FILE=docker-compose.prod.yml ${COMPOSE_CMD} logs"
    exit 1
fi

# ── Summary ──────────────────────────────────────────────────────────────────

printf "\n${GREEN}${BOLD}Modus is running!${NC}\n\n"
printf "  ${BOLD}Dashboard${NC}:  http://localhost:${PORT}\n"
# The interactive API docs are only served outside production (orchestrator/main.py);
# with the shipped production env the URL would be a 404, so only print it for development.
ENVIRONMENT=$(grep '^MODUS_ENVIRONMENT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '" \r')
if [ "${ENVIRONMENT}" = "development" ]; then
    printf "  ${BOLD}API docs${NC}:   http://localhost:${PORT}/docs\n"
fi
printf "  ${BOLD}Health${NC}:     http://localhost:${PORT}/health\n"
printf "  ${BOLD}Config${NC}:     ${DEPLOY_DIR}/.env\n"

printf "\n${BOLD}Next steps:${NC}\n"
printf "  1. Install the SDK from this checkout: ${BLUE}pip install ../sdk${NC}\n"
printf "  2. Point your app at Modus: ${BLUE}export MODUS_URL=http://localhost:${PORT}${NC}\n"
printf "  3. Open the dashboard: ${BLUE}http://localhost:${PORT}${NC}\n"

printf "\n${BOLD}Management:${NC}\n"
printf "  Upgrade:   ${BLUE}${DEPLOY_DIR}/upgrade.sh${NC}\n"
printf "  Uninstall: ${BLUE}${DEPLOY_DIR}/uninstall.sh${NC}\n\n"
