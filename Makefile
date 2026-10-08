# Modus — Developer Makefile
# ====================================
# Run from the repo root. Targets operate on docker-compose.yml, which defines
# two services: `modus` (orchestrator + dashboard, port 8080) and `db`
# (PostgreSQL 16, user `postgres`).
#
# First time setup:
#   make up      — build and start the stack (modus + db)
#   make seed    — create a dev team, token, and sample policies
#                  (authenticates with MODUS_MASTER_API_KEY from .env, so it works in
#                  stub and jwt auth modes; it also writes .env.agents)
#
# Daily use:
#   make up      — start (or restart) the stack
#   make logs    — follow all logs
#   make down    — stop (keep data)
#   make reset   — stop and wipe all data (fresh start)

COMPOSE = docker compose -f docker-compose.yml
ENV_FILE = .env

.DEFAULT_GOAL := help

.PHONY: help up down reset build seed logs logs-modus db db-reset shell ps test

# ── Core stack ─────────────────────────────────────────────────────────────────

up: ## Start the stack (modus + db)
	@echo "Starting Modus dev stack..."
	$(COMPOSE) up -d
	@echo ""
	@echo "   Orchestrator:  http://localhost:8080"
	@echo "   API docs:      http://localhost:8080/docs"
	@echo "   Dashboard:     http://localhost:8080  (served by the orchestrator)"
	@echo ""
	@echo "   Next: make seed"

down: ## Stop all containers (data preserved)
	$(COMPOSE) down

reset: ## Stop and delete all data volumes (fresh start)
	@echo "This will delete all data. Ctrl+C to cancel..."
	@sleep 3
	$(COMPOSE) down -v

build: ## Rebuild the orchestrator image (after code changes)
	$(COMPOSE) build modus

# ── Seeding ────────────────────────────────────────────────────────────────────

seed: ## Create dev team, token, sample policies (uses the master key from .env)
	@echo "Seeding dev data..."
	@MODUS_ORCHESTRATOR_URL=http://localhost:8080 \
	  MODUS_MASTER_API_KEY=$$(grep '^MODUS_MASTER_API_KEY=' $(ENV_FILE) | cut -d= -f2) \
	  python3 scripts/seed.py

# ── Logs ───────────────────────────────────────────────────────────────────────

logs: ## Tail all logs
	$(COMPOSE) logs -f --tail=50

logs-modus: ## Tail orchestrator logs only
	$(COMPOSE) logs -f --tail=100 modus

# ── Database ───────────────────────────────────────────────────────────────────

db: ## Open psql shell against the database
	$(COMPOSE) exec db psql -U postgres modus

db-reset: ## Drop and recreate the database (nuclear option)
	$(COMPOSE) exec db psql -U postgres -c "DROP DATABASE IF EXISTS modus; CREATE DATABASE modus;"

# ── Shell access ───────────────────────────────────────────────────────────────

shell: ## Open a shell inside the orchestrator container
	$(COMPOSE) exec modus sh

# ── Status ─────────────────────────────────────────────────────────────────────

ps: ## Show container status
	$(COMPOSE) ps

# ── Tests ──────────────────────────────────────────────────────────────────────

test: ## Run unit tests
	python3 -m pytest tests/ -v

# ── Help ───────────────────────────────────────────────────────────────────────

help: ## Show this help
	@echo ""
	@echo "Modus Dev Commands"
	@echo "========================="
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
