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
#   ./deploy.sh --status        # Check health (add --full for the full stack)
#   ./deploy.sh --systemd       # Install systemd units (bare metal, no Docker)
#   ./deploy.sh --status --systemd  # Check a bare-metal install
#
# Prerequisites:
#   - Docker and Docker Compose installed (not needed for --systemd, which
#     needs systemd, python3 with venv, tar and curl; run it as root or with sudo)
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
[[ " $* " == *" --systemd "* ]] && MODE="systemd"

# Bare-metal (--systemd) layout; the units in systemd/ use these paths.
APP_DIR=/opt/federator
DATA_DIR=/data

# Run a command as root: directly when already root, otherwise via sudo.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# ── Shared checks ────────────────────────────────────────────────────────────

check_env() {
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

    if ! grep -Eq '^FEDERATOR_JWT_SECRET=.+' .env; then
        err "FEDERATOR_JWT_SECRET is not set in .env."
        exit 1
    fi
}

check_prereqs() {
    if ! command -v docker &>/dev/null; then
        err "Docker not found. Install: https://docs.docker.com/engine/install/"
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        err "Docker Compose not found."
        exit 1
    fi

    check_env
}

# The domain Caddy serves (FEDERATOR_DOMAIN in .env).
federator_domain() {
    local domain=""
    if [ -f .env ]; then
        domain=$(grep -E '^FEDERATOR_DOMAIN=' .env | tail -n 1 | cut -d= -f2- | tr -d '"'"'"'[:space:]')
    fi
    echo "${domain:-federator.example.com}"
}

# GET /health through Caddy: HTTPS on 443 for FEDERATOR_DOMAIN, resolved to
# this host so it works before DNS does. The certificate is verified first;
# an untrusted one (e.g. Caddy's internal CA for localhost) is reported.
check_public_health() {
    local domain body rc
    domain=$(federator_domain)
    body=$(curl -sS --max-time 5 --resolve "${domain}:443:127.0.0.1"         "https://${domain}/health" 2>/dev/null) && rc=0 || rc=$?
    if [ "$rc" -eq 0 ]; then
        log "HTTPS https://${domain}/health (via Caddy on 443): OK ${body}"
        return 0
    fi
    if [ "$rc" -eq 60 ] && body=$(curl -sS -k --max-time 5             --resolve "${domain}:443:127.0.0.1" "https://${domain}/health" 2>/dev/null); then
        warn "HTTPS https://${domain}/health (via Caddy on 443): responds, but its TLS certificate is not trusted: ${body}"
        return 0
    fi
    warn "HTTPS https://${domain}/health (via Caddy on 443): no response (curl exit ${rc})"
    return 1
}

# GET /health on the service behind Caddy, by the address it really listens on.
check_backend_health() {
    local body state
    case "$MODE" in
        full)
            # The federator container publishes plain HTTP on 8443.
            if body=$(curl -sS --max-time 5 http://127.0.0.1:8443/health 2>/dev/null); then
                log "Federator http://127.0.0.1:8443/health: OK ${body}"
                return 0
            fi
            warn "Federator http://127.0.0.1:8443/health: no response"
            ;;
        slim)
            # The writer's port is not published; use its container health check.
            state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no healthcheck{{end}}'                 modus-writer 2>/dev/null || echo "not running")
            if [ "$state" = "healthy" ]; then
                log "Writer container: healthy"
                return 0
            fi
            warn "Writer container: ${state}"
            ;;
        systemd)
            # The writer socket (plain HTTP, loopback only) starts it on demand.
            if body=$(curl -sS --max-time 10 http://127.0.0.1:8444/health 2>/dev/null); then
                log "Writer http://127.0.0.1:8444/health: OK ${body}"
                return 0
            fi
            warn "Writer http://127.0.0.1:8444/health: no response"
            ;;
    esac
    return 1
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

    log "Checking health..."
    local healthy=false
    for _ in $(seq 1 30); do
        if check_backend_health >/dev/null 2>&1; then
            healthy=true
            break
        fi
        sleep 2
    done
    if [ "$healthy" = true ]; then
        check_backend_health || true
        check_public_health || warn "Caddy is not serving HTTPS yet (DNS or certificate?) — check: docker logs modus-caddy"
        log "Federator is healthy!"
    else
        check_backend_health || true
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
    log "Checking Modus Federator status (${MODE} mode)..."
    echo ""

    case "$MODE" in
        slim)    docker compose -f docker-compose.slim.yml ps ;;
        full)    docker compose ps ;;
        systemd) systemctl --no-pager list-units 'federator-*' --all ;;
    esac

    echo ""
    local ok=true
    check_backend_health || ok=false
    check_public_health || ok=false

    if [ "$MODE" = "systemd" ]; then
        if [ -f "${DATA_DIR}/static/portal.json" ]; then
            info "Static files: present"
        else
            warn "Static files: ${DATA_DIR}/static/portal.json missing (run: systemctl start federator-aggregate.service)"
        fi
    else
        echo ""
        info "Container resource usage:"
        docker stats --no-stream --format "  {{.Name}}: {{.MemUsage}} / CPU {{.CPUPerc}}" 2>/dev/null || true
    fi

    [ "$ok" = true ]
}

install_systemd() {
    log "Installing systemd units for bare-metal deployment..."

    for cmd in systemctl python3 tar curl; do
        if ! command -v "$cmd" &>/dev/null; then
            err "${cmd} not found; --systemd needs systemd, python3 (with venv), tar and curl."
            exit 1
        fi
    done
    check_env

    # Service user: owns the data, not the code
    if ! id -u federator &>/dev/null; then
        as_root useradd --system --user-group --shell /usr/sbin/nologin             --home-dir "$APP_DIR" --no-create-home federator
        log "Created federator user"
    fi

    # Directories: code is root-owned and read-only to the service
    as_root install -d -m 755 -o root -g root "$APP_DIR"
    as_root install -d -m 755 -o federator -g federator         "$DATA_DIR" "${DATA_DIR}/static" "${DATA_DIR}/static/results" "${DATA_DIR}/static/audit"

    # Python venv with the slim dependencies (re-run on every install so an
    # update picks up new pins)
    if [ ! -x "${APP_DIR}/venv/bin/python" ]; then
        as_root python3 -m venv "${APP_DIR}/venv"
        log "Created Python venv"
    fi
    as_root "${APP_DIR}/venv/bin/pip" install --quiet --no-cache-dir -r requirements-slim.txt

    # Application: this directory is the `federator` package. Copy it to
    # ${APP_DIR}/federator without secrets, databases or deployment files
    # (the same exclusions as .dockerignore), then swap it in.
    local staging="${APP_DIR}/federator.new"
    as_root rm -rf "$staging"
    as_root install -d -m 755 -o root -g root "$staging"
    tar -C "$SCRIPT_DIR"         --exclude='.env' --exclude='.env.*' --exclude='*.db' --exclude='*.db-*'         --exclude='__pycache__' --exclude='*.pyc' --exclude='Dockerfile*'         --exclude='docker-compose*.yml' --exclude='Caddyfile*' --exclude='deploy.sh'         --exclude='systemd' --exclude='.dockerignore'         -cf - . | as_root tar -C "$staging" --no-same-owner -xf -
    as_root rm -rf "${APP_DIR}/federator"
    as_root mv "$staging" "${APP_DIR}/federator"
    as_root "${APP_DIR}/venv/bin/python" -m compileall -q "${APP_DIR}/federator"

    # Settings: readable by the service, not by other users
    as_root install -m 640 -o root -g federator .env "${APP_DIR}/.env"

    # systemd units
    for unit in federator-writer.socket federator-writer.service                 federator-aggregate.timer federator-aggregate.service; do
        as_root install -m 644 -o root -g root "systemd/${unit}" "/etc/systemd/system/${unit}"
    done

    as_root systemctl daemon-reload
    as_root systemctl enable --now federator-writer.socket
    as_root systemctl enable --now federator-aggregate.timer
    # Pick up new code and settings if the writer was already running
    as_root systemctl try-restart federator-writer.service

    # The first connection starts the writer, which creates the database;
    # then publish the initial static files.
    if check_backend_health; then
        if as_root systemctl start federator-aggregate.service; then
            log "Initial aggregation published to ${DATA_DIR}/static"
        else
            warn "Initial aggregation failed — check: journalctl -u federator-aggregate"
        fi
    else
        err "The writer did not start — check: journalctl -u federator-writer"
        exit 1
    fi

    log "Systemd units installed and enabled."
    info "  Writer:     systemctl status federator-writer.socket federator-writer.service"
    info "  Aggregator: systemctl status federator-aggregate.timer"
    info "  Logs:       journalctl -u federator-writer -f"
    info "  Caddy is not installed by this script: install it, use federator/Caddyfile"
    info "  and set FEDERATOR_DOMAIN in Caddy's environment (see docs/deployment.md)."
}

# ── Main ─────────────────────────────────────────────────────────────────────

if [ "$MODE" = "systemd" ] && [[ "${1:-setup}" =~ ^-*(setup|update)$ ]]; then
    err "--setup and --update deploy with Docker; for bare metal run: $0 --systemd"
    exit 1
fi

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
        echo "  --status    Check health and resource usage; exits 1 if unhealthy"
        echo "              (add --full or --systemd for those installs)"
        echo "  --systemd   Install systemd units (bare metal, no Docker)"
        echo "  --full      Use full FastAPI stack instead of slim mode"
        exit 1
        ;;
esac
