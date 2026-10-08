#!/usr/bin/env bash
# =============================================================================
# Modus Uninstaller
# =============================================================================
# Stops containers, optionally removes data and images.
# By default, data is KEPT (safe uninstall). Use --purge to remove everything.
#
# Usage:
#   ./uninstall.sh           # Stop containers, keep data
#   ./uninstall.sh --purge   # Remove everything including data
#
# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
# =============================================================================

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
PURGE=false

# Colors
if [ -t 1 ]; then
    RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m'
    BOLD='\033[1m' NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BOLD='' NC=''
fi

log()   { printf "${GREEN}[uninstall]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[uninstall]${NC} %s\n" "$*"; }
error() { printf "${RED}[uninstall]${NC} %s\n" "$*" >&2; }

# ── Parse Arguments ──────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--purge]"
            echo "  --purge  Remove all data, volumes, and images (default: keep data)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Detect Compose Command ──────────────────────────────────────────────────

COMPOSE_CMD=""
if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    error "Docker Compose not found. Attempting manual cleanup..."
    docker stop modus-orchestrator modus-conductor 2>/dev/null || true
    docker rm modus-orchestrator modus-conductor 2>/dev/null || true
    log "Containers removed."
    exit 0
fi

cd "${INSTALL_DIR}"

# ── Confirmation ─────────────────────────────────────────────────────────────

printf "\n${BOLD}Modus Uninstaller${NC}\n\n"

if [ "${PURGE}" = true ]; then
    printf "${RED}WARNING: --purge will permanently delete ALL Modus data!${NC}\n"
    printf "This includes:\n"
    printf "  - All cost tracking data and analytics\n"
    printf "  - Application registrations and policies\n"
    printf "  - Database backups in ${INSTALL_DIR}/backups/\n"
    printf "  - Docker volumes\n\n"
    printf "Type 'yes' to confirm permanent deletion: "
    read -r CONFIRM
    if [ "${CONFIRM}" != "yes" ]; then
        log "Aborted."
        exit 0
    fi
else
    printf "This will stop Modus containers but ${BOLD}keep your data${NC}.\n"
    printf "Use --purge to also remove all data.\n\n"
    printf "Continue? [y/N] "
    read -r REPLY
    if [ "${REPLY}" != "y" ] && [ "${REPLY}" != "Y" ]; then
        log "Aborted."
        exit 0
    fi
fi

# ── Stop Containers ──────────────────────────────────────────────────────────

log "Stopping Modus containers..."

if [ -f docker-compose.yml ] || [ -f docker-compose.prod.yml ]; then
    [ -f docker-compose.yml ] || export COMPOSE_FILE=docker-compose.prod.yml
    ${COMPOSE_CMD} --profile org-wide down 2>/dev/null || ${COMPOSE_CMD} down 2>/dev/null || true
else
    docker stop modus-orchestrator modus-conductor 2>/dev/null || true
    docker rm modus-orchestrator modus-conductor 2>/dev/null || true
fi

log "Containers stopped."

# ── Remove Docker Images ────────────────────────────────────────────────────

log "Removing Modus Docker images..."
docker rmi ghcr.io/babbguy/modus-orchestrator 2>/dev/null || true
docker rmi ghcr.io/babbguy/modus-conductor 2>/dev/null || true
# Remove all tagged versions
docker images --format '{{.Repository}}:{{.Tag}}' | grep 'babbguy/Modus' | xargs -r docker rmi 2>/dev/null || true
log "Images removed."

# ── Remove Volumes (purge only) ─────────────────────────────────────────────

if [ "${PURGE}" = true ]; then
    log "Removing Docker volumes..."
    docker volume rm modus_data conductor_data 2>/dev/null || true
    # Also try project-prefixed volume names
    docker volume ls --format '{{.Name}}' | grep modus | xargs -r docker volume rm 2>/dev/null || true
    log "Volumes removed."
fi

# ── Remove local configuration and backups (purge only) ─────────────────────
# This script lives inside a source checkout, so the directory itself is never
# deleted; only the generated .env and the backups/ directory are removed.

if [ "${PURGE}" = true ]; then
    printf "
Remove ${INSTALL_DIR}/.env and ${INSTALL_DIR}/backups? [y/N] "
    read -r REPLY
    if [ "${REPLY}" = "y" ] || [ "${REPLY}" = "Y" ]; then
        rm -f "${INSTALL_DIR}/.env"
        rm -rf "${INSTALL_DIR}/backups"
        log "Removed .env and backups/"
    else
        log "Configuration and backups kept."
    fi
else
    log "Data preserved at ${INSTALL_DIR}"
    log "  Database:  Docker volume 'modus_data'"
    log "  Backups:   ${INSTALL_DIR}/backups/"
    log "  Config:    ${INSTALL_DIR}/.env"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

printf "\n${GREEN}${BOLD}Modus uninstalled.${NC}\n"
if [ "${PURGE}" = false ]; then
    printf "  Data has been preserved. Reinstall with: ./install.sh\n"
fi
printf "\n"
