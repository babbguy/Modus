#!/usr/bin/env bash
# Modus Federator — VPS Deployment Script
# =============================================
# Two deployment modes:
#   --slim   Static-first architecture ($3-5/mo VPS, ~15MB idle RAM)
#   --full   Full FastAPI stack ($10-20/mo VPS, ~200MB idle RAM)
#
# Usage:
#   ./deploy.sh --setup         # Deploy slim mode (default)
#   ./deploy.sh --setup --full  # Deploy full mode
#   ./deploy.sh --update        # Rebuild and restart
#   ./deploy.sh --status        # Check health
#   ./deploy.sh --systemd       # Install systemd units (bare metal, no Docker)
#
# Prerequisites:
#   - Docker and Docker Compose installed
#   - Domain pointing to this VPS (for TLS via Caddy)
#   - .env file configured (copy from .env.example)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[federator]${NC} $1"; }
warn() { echo -e "${YELLOW}[federator]${NC} $1"; }
err()  { echo -e "${RED}[federator]${NC} $1" >&2; }
info() { echo -e "${CYAN}[federator]${NC} $1"; }

MODE="slim"
[[ " $* " == *" --full "* ]] && MODE="full"

# ── Shared checks ────────────────────────────────────────────────────────────

check_prereqs() {
    if ! command -v docker &>/dev/null; then
        err "Docker not found. Install: https://docs.docker.com/engine/install/"
        exit 1
    fi
    if ! command -v docker compose &>/dev/null && ! command -v docker-compose &>/dev/null; then
        err "Docker Compose not found."
        exit 1
    fi

    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            warn "Created .env from .env.example — EDIT IT before proceeding!"
            warn "At minimum, set FEDERATOR_JWT_SECRET to a strong random value."
            exit 1
        else
            err "No .env or .env.example found."
            exit 1
        fi
    fi

    if grep -q "change-me" .env 2>/dev/null; then
        err "FEDERATOR_JWT_SECRET is still the default value. Change it!"
        exit 1
    fi
}

# ── Commands ─────────────────────────────────────────────────────────────────

setup() {
    check_prereqs

    if [ "$MODE" = "slim" ]; then
        log "Deploying SLIM mode (static-first architecture)"
        info "  Idle RAM:  ~15MB (Caddy only, no Python)"
        info "  Peak RAM:  ~70MB (during writes)"
        info "  Target:    \$3-5/mo VPS"
        echo ""

        docker compose -f docker-compose.slim.yml build --no-cache
        docker compose -f docker-compose.slim.yml up -d

        # Trigger initial aggregation
        sleep 2
        log "Running initial aggregation..."
        docker compose -f docker-compose.slim.yml exec -T aggregator \
            python -m federator.core.static_publisher 2>/dev/null || true
    else
        log "Deploying FULL mode (FastAPI stack)"
        info "  Idle RAM:  ~200MB"
        info "  Target:    \$10-20/mo VPS"
        echo ""

        docker compose build --no-cache
        docker compose up -d
    fi

    sleep 3
    log "Checking health..."
    if curl -sf http://localhost:443/health 2>/dev/null || curl -sf http://localhost:8443/health 2>/dev/null || curl -sf http://localhost:8444/health 2>/dev/null; then
        echo ""
        log "Federator is healthy!"
    else
        warn "Health check not responding yet — check logs"
    fi

    echo ""
    log "Setup complete."
    log "Endpoints:"
    log "  Health:     GET  /health"
    log "  Portal:     GET  /v1/portal"
    log "  Submit:     POST /v1/deltas"
    log "  Results:    GET  /v1/results"
    log "  Benchmarks: GET  /v1/benchmarks"
    log "  Audit:      GET  /v1/audit/merkle-root"

    if [ "$MODE" = "slim" ]; then
        echo ""
        info "Slim mode: reads are served as static JSON by Caddy."
        info "Aggregation runs hourly to refresh results."
    fi
}

update() {
    if [ "$MODE" = "slim" ]; then
        docker compose -f docker-compose.slim.yml build --no-cache
        docker compose -f docker-compose.slim.yml up -d
    else
        docker compose build --no-cache
        docker compose up -d
    fi
    log "Update complete."
}

status() {
    log "Checking Modus Federator status..."
    echo ""

    if [ "$MODE" = "slim" ]; then
        docker compose -f docker-compose.slim.yml ps
    else
        docker compose ps
    fi

    echo ""

    # Try various ports
    for port in 443 8443 8444; do
        if curl -sf "http://localhost:${port}/health" 2>/dev/null; then
            echo ""
            log "Health (port ${port}): OK"
            break
        fi
    done

    # Check static files exist (slim mode)
    if [ -f /data/static/portal.json ] 2>/dev/null; then
        info "Static files: present"
    fi

    # Show resource usage
    echo ""
    info "Container resource usage:"
    docker stats --no-stream --format "  {{.Name}}: {{.MemUsage}} / CPU {{.CPUPerc}}" 2>/dev/null || true
}

install_systemd() {
    log "Installing systemd units for bare-metal deployment..."

    # Create user
    if ! id -u federator &>/dev/null; then
        sudo useradd -r -s /bin/false -d /opt/federator federator
        log "Created federator user"
    fi

    # Create directories
    sudo mkdir -p /opt/federator /data/static/results /data/static/audit
    sudo chown -R federator:federator /data

    # Create venv
    if [ ! -d /opt/federator/venv ]; then
        sudo -u federator python3 -m venv /opt/federator/venv
        sudo -u federator /opt/federator/venv/bin/pip install -r requirements-slim.txt
        log "Created Python venv"
    fi

    # Copy application
    sudo cp -r federator/ /opt/federator/
    sudo cp .env /opt/federator/.env 2>/dev/null || true

    # Install systemd units
    sudo cp systemd/federator-writer.socket /etc/systemd/system/
    sudo cp systemd/federator-writer.service /etc/systemd/system/
    sudo cp systemd/federator-aggregate.timer /etc/systemd/system/
    sudo cp systemd/federator-aggregate.service /etc/systemd/system/

    sudo systemctl daemon-reload
    sudo systemctl enable --now federator-writer.socket
    sudo systemctl enable --now federator-aggregate.timer

    log "Systemd units installed and enabled."
    info "  Writer:     systemctl status federator-writer.socket"
    info "  Aggregator: systemctl status federator-aggregate.timer"
    info "  Logs:       journalctl -u federator-writer -f"
}

# ── Main ─────────────────────────────────────────────────────────────────────

case "${1:-setup}" in
    --setup|setup)     setup ;;
    --update|update)   update ;;
    --status|status)   status ;;
    --systemd)         install_systemd ;;
    *)
        echo "Usage: $0 [--setup | --update | --status | --systemd] [--full]"
        echo ""
        echo "  --setup     Deploy (default: slim mode)"
        echo "  --update    Rebuild and restart"
        echo "  --status    Check health and resource usage"
        echo "  --systemd   Install systemd units (bare metal, no Docker)"
        echo "  --full      Use full FastAPI stack instead of slim mode"
        exit 1
        ;;
esac
