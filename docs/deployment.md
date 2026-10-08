# Modus — Deployment Guide

Modus is self-hosted. There are no published container images yet (they are
produced by the `publish` workflow when a `v*` tag is pushed), so every option
below builds from a clone of the repository:

```bash
git clone https://github.com/babbguy/Modus.git
cd Modus
```

## Deployment options

### Option 1: Docker Compose with PostgreSQL

Uses `docker-compose.yml` (orchestrator + PostgreSQL 16).

```bash
cp .env.example .env
# Edit .env: at minimum set MODUS_MASTER_API_KEY and POSTGRES_PASSWORD.
# For a throwaway local trial also set MODUS_ENVIRONMENT=development and
# MODUS_AUTH_MODE=stub (see "Authentication" below).
docker compose up --build -d
# Open http://localhost:8080
```

The Compose file requires `.env` to exist (`env_file: .env`).

What you get:

- A `modus` service (the orchestrator, which also serves the dashboard on port
  8080) and a `db` service (`postgres:16-alpine`, published only on
  `127.0.0.1:5432`).
- Migrations on startup: for PostgreSQL the image entrypoint runs
  `alembic upgrade heads` before the app boots (SQLite creates its tables
  automatically).
- The local `plugins/` directory mounted into the container.
- Health checks and memory/CPU limits.

`make up`, `make logs`, `make down`, `make seed` and the other `make` targets
wrap this Compose file (run `make help`).

### Option 2: Docker Compose, single container with SQLite

Uses `docker-compose.standalone.yml`. No PostgreSQL; the database is a file in
the `modus_data` volume.

```bash
cp .env.example .env     # edit as above
docker compose -f docker-compose.standalone.yml up --build -d
```

### Option 3: Run directly with Python

Requires Python 3.10 or newer (the pinned dependencies do not install on 3.9).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r orchestrator/requirements.txt

export MODUS_DATABASE_URL=sqlite+aiosqlite:///modus.db
export MODUS_MASTER_API_KEY=mds_master_$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export MODUS_ENVIRONMENT=development      # with the default MODUS_AUTH_MODE=stub
python -m uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080
```

SQLite tables are created automatically. For PostgreSQL, point
`MODUS_DATABASE_URL` at a `postgresql+asyncpg://` URL and run
`alembic upgrade heads` first.

Not every feature works on SQLite: the z-score anomaly scan requires
PostgreSQL and is skipped on SQLite.

### Option 4: Production Compose files and install scripts (`deploy/`)

`deploy/docker-compose.prod.yml` runs the orchestrator (SQLite by default,
volume `modus_data`) and, with the `org-wide` profile, a Conductor on port 8090.
Run it from the `deploy/` directory:

```bash
cd deploy
./install.sh               # copies .env.example to .env, generates a master key,
                           # builds the images and waits for /health
./install.sh --org-wide    # also starts the Conductor
./upgrade.sh               # git pull --ff-only, back up the database, rebuild, restart; rolls back if the health check fails
./uninstall.sh [--purge]   # stop containers (--purge also removes data)
```

The scripts build from your checkout. `deploy/.env.example` ships
`MODUS_ENVIRONMENT=production` and `MODUS_AUTH_MODE=jwt`, so set
`MODUS_JWT_SECRET` (see "Authentication"); the interactive API docs are
disabled in production mode.

### Option 5: Kubernetes (Helm)

The chart is in `deploy/helm/modus`. It deploys the orchestrator as a
`Deployment` (a `migrate` init container runs `alembic upgrade heads`, and the
main container sets `MODUS_SKIP_MIGRATIONS=1`) with a `Service`, optional
`Ingress`, `HorizontalPodAutoscaler`, `PodDisruptionBudget` and
`ServiceMonitor` and (opt-in) `NetworkPolicy`. It requires an external
PostgreSQL database and three secret values, which the chart reads from
existing Kubernetes secrets:

```bash
kubectl create secret generic modus-db \
  --from-literal=url="postgresql+asyncpg://user:pass@host:5432/modus"
kubectl create secret generic modus-master-key \
  --from-literal=key="mds_master_$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl create secret generic modus-jwt \
  --from-literal=secret="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

helm install modus ./deploy/helm/modus \
  --set orchestrator.image.repository=<your-registry>/modus-orchestrator \
  --set orchestrator.image.tag=<tag>
```

Build and push the image yourself first (`docker build -f
Dockerfile.orchestrator -t <your-registry>/modus-orchestrator:<tag> .`), or use
the `ghcr.io/babbguy/modus-orchestrator` image after a tagged release has been
published.

`authMode` defaults to `jwt`, so `MODUS_JWT_SECRET` is injected from the
`auth.jwt.existingSecret` secret (`modus-jwt`, key `secret`); set
`orchestrator.config.jwtIssuer` / `jwtAudience` to match your tokens. Instead of
pre-creating secrets you can pass `--set secrets.create=true` together with
`secrets.databaseUrl`, `secrets.masterApiKey` and `secrets.jwtSecret`
(`--set-string`, or `--set-file` for the values); they then live in the Helm
release, so prefer your usual secret tooling for production.

The orchestrator serves the dashboard itself, so the chart has no separate
dashboard or relay: reach it through the orchestrator `Service`, or enable
`orchestrator.ingress`, on port 8080. With `networkPolicy.enabled=true`, ingress
to the orchestrator pods is limited to the peers listed in
`networkPolicy.allowedPeers` (your ingress controller, Prometheus, the
namespaces your applications run in); with an empty list only pods in the
release namespace may connect. The chart passes `helm lint` and `helm template`
(including `authMode=stub`, `secrets.create=true` and `networkPolicy.enabled`);
it is not exercised in CI.

### Option 6: Platforms that build a Dockerfile

Any platform that builds the repository's `Dockerfile` can run Modus. Expose
port `8080`, use `/health` for the health check, and set the environment
variables below. Persist `/app/data` if you use SQLite.

## Authentication

`MODUS_AUTH_MODE` selects how user-facing API calls (the dashboard, teams,
policies, pricing, notifications, and so on) are authenticated.

- `stub`: no authentication; every request is a platform admin. The
  orchestrator refuses to start with it unless `MODUS_ENVIRONMENT=development`.
  This is the code default, intended only for local trials.
- `jwt`: each request carries `Authorization: Bearer <JWT>`. Tokens are verified
  with `MODUS_JWT_SECRET` (HS256; RS256 if the value is a PEM key). Required
  claims are `sub`, `exp` and `aud` (which must equal `MODUS_JWT_AUDIENCE`,
  default `modus`); `MODUS_JWT_ISSUER` is checked if set. Optional claims are
  `cs_role` (`platform_admin`, `team_admin`, `team_member` or `read_only`, the
  default) and `cs_teams` (list of team slugs). Modus has no login endpoint of
  its own: you issue these tokens from your own identity provider or a script.
  For example, an admin token for `curl` (PyJWT is installed with the
  orchestrator requirements):

  ```bash
  python -c "import jwt, time; print(jwt.encode({'sub': 'admin', 'aud': 'modus', 'exp': int(time.time()) + 3600, 'cs_role': 'platform_admin'}, '$MODUS_JWT_SECRET', algorithm='HS256'))"
  ```

  The dashboard's sign-in screen accepts such a token (sent as
  `Authorization: Bearer`) or the master key (below). The credential is kept in
  `sessionStorage` for the tab only and is never read from the URL.

The master key (`MODUS_MASTER_API_KEY`, sent as `X-Modus-APIKey`) is a **root
credential**: in either auth mode it authenticates as a platform-admin service
identity (audit actor `master-key`) for every user-facing admin endpoint, and
it is also what app registration, key rotation
(`POST /api/v1/apps/{id}/rotate-key`) and team registration tokens require. The
policy CLI (`python -m modus policy ...`), the GitHub Action, `scripts/seed.py`
and the dashboard sign-in all use it. A wrong key is rejected with HTTP 401 in
`jwt` mode. Keep it in a secret store, never in a repository or a browser you
do not control, and rotate it by changing `MODUS_MASTER_API_KEY` (this also
invalidates session tokens, which are derived from it).

SDK agents authenticate with `X-Modus-APIKey` carrying either a stable `mds_`
app key (from app registration) or an `mst_` session token that the SDK obtains
by calling `POST /api/v1/self-register` with a team registration token
(`X-Modus-TeamToken: mds_team_...`). Typical onboarding in `jwt` mode:

```bash
# 1. Create a team (admin JWT, or the master key via -H "X-Modus-APIKey: $MASTER_KEY")
curl -s -X POST "$URL/api/v1/teams" -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" -d '{"slug": "ml-platform", "name": "ML Platform"}'

# 2. Issue the team's registration token (master key; shown once)
curl -s -X POST "$URL/api/v1/teams/<team id>/registration-token" \
  -H "X-Modus-APIKey: $MODUS_MASTER_API_KEY"

# 3. Give developers MODUS_URL and MODUS_TEAM_TOKEN; their apps self-register.
```

## Environment variables

Every setting is a field of `Settings` in `orchestrator/core/config.py`, read
from `MODUS_<FIELD_NAME>` (case-insensitive) or from a `.env` file. The most
important ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODUS_DATABASE_URL` | `sqlite+aiosqlite:///modus.db` | `postgresql+asyncpg://...` or `sqlite+aiosqlite:///...` |
| `MODUS_MASTER_API_KEY` | placeholder (refused when `MODUS_ENVIRONMENT=production`) | Must start with `mds_master_`; at least 20 characters |
| `MODUS_ENVIRONMENT` | `development` | `development`, `staging` or `production` |
| `MODUS_AUTH_MODE` | `stub` | `stub` (local development only) or `jwt` |
| `MODUS_JWT_SECRET` | empty | Required for `jwt` mode |
| `MODUS_JWT_AUDIENCE` / `MODUS_JWT_ISSUER` | `modus` / empty | JWT claims to enforce |
| `MODUS_PORT` | `8080` | Listen port (only when started with `python -m orchestrator.main`; with `uvicorn` the `--port` flag applies) |
| `MODUS_LOG_FORMAT` | `json` | `json` or `text` |
| `MODUS_CORS_ORIGINS` | `http://localhost:3001,http://localhost:8000,http://localhost:8080` | Comma-separated CORS origins |
| `MODUS_ENCRYPTION_KEY` | empty | Fernet key for stored credentials; if empty they are stored unencrypted |
| `MODUS_ENFORCEMENT_FAIL_OPEN` | `false` | Gateway and `/api/v1/evaluate/`: allow on timeout/error |
| `MODUS_GATEWAY_ENABLED` | `false` | Mount the gateway proxy under `/gateway` |
| `MODUS_DISABLE_DASHBOARD` | `false` | Do not serve the dashboard |
| `MODUS_PLUGINS_ENABLED` | `true` | Load `plugins/*.py` at startup |
| `MODUS_PARTITION_USAGE_RECORDS` | `false` | Opt-in monthly partitioning of `usage_records` (PostgreSQL); the baseline migration creates a plain table, so converting it to a partitioned table is a manual step |
| `MODUS_PRICING_LIVE_FETCH_ENABLED` | `false` | Allow price refresh from provider APIs |
| `MODUS_NOMUS_URL` | empty | Enable the optional Nomus integration |
| `MODUS_CONDUCTOR_URL` | empty | Push aggregates to a Conductor (must resolve to a private address) |

See `.env.example` for a longer list with comments.

## Using the evaluate endpoint directly

Applications that do not use the SDK or the gateway can ask for a decision
before each provider call. In `jwt` mode this endpoint needs a bearer token;
`app_id` is the app's slug.

```python
import httpx

async def call_ai_with_enforcement(prompt: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://modus:8080/api/v1/evaluate/",
            json={
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "input_tokens": len(prompt.split()) * 2,  # rough estimate
                "output_tokens": 500,
                "app_id": "your-app-slug",
                "session_id": "agent-session-123",  # optional: for session budgets
                "environment": "production",
            },
        )
    result = resp.json()   # decision: allow | deny | throttle, plus reason

    if result["decision"] != "allow":
        raise RuntimeError(result["reason"])

    # ...then call the real AI provider
```

## Session budgets (agent runs)

For multi-step agents, create a budget for a session and pass its `session_id`
on each evaluate call; Modus tracks cumulative spend and denies once the
session budget is exceeded.

```python
await client.post(
    "http://modus:8080/api/v1/evaluate/sessions",
    json={
        "session_id": "agent-run-abc123",
        "app_id": "your-app-slug",
        "max_budget_usd": 5.00,
    },
)
```

## Plugins

Python files in `plugins/` (except names starting with `_`) are imported at
startup, and `on_load()` is called if the module defines it. That is the only
hook at present; see `plugins/example_custom_policy.py`.

## Backup and restore

Back up before every upgrade. `deploy/upgrade.sh` does this automatically for
the `deploy/` setup and detects the backend; the manual commands are below. The
examples assume the container name `modus-orchestrator` from
`deploy/docker-compose.prod.yml`; with the root Compose files use the name
shown by `docker compose ps`.

### SQLite

The whole database is one file inside the `modus_data` Docker volume
(`/app/data/modus.db`).

```bash
# Back up (copy the file out of the running container)
docker cp modus-orchestrator:/app/data/modus.db ./modus-backup.db

# Restore
docker cp ./modus-backup.db modus-orchestrator:/app/data/modus.db
docker restart modus-orchestrator
```

### PostgreSQL

Use `pg_dump` for a logical, restorable dump. Run it from any host that can
reach the database; here, a throwaway client container that shares the
orchestrator's network namespace:

```bash
# Back up (plain SQL; drops and recreates objects on restore)
docker run --rm --network container:modus-orchestrator postgres:16-alpine \
  pg_dump --no-owner --no-privileges --clean --if-exists \
  "postgresql://user:password@host:5432/modus" > modus-backup.sql

# Restore the plain-SQL dump
docker run --rm -i --network container:modus-orchestrator postgres:16-alpine \
  psql "postgresql://user:password@host:5432/modus" < modus-backup.sql
```

For the compressed custom format, dump with `pg_dump -Fc` and restore with
`pg_restore --clean --if-exists -d "<dsn>"` instead of `psql`.

> Use the plain `postgresql://` scheme for `pg_dump`/`psql`: strip the
> `+asyncpg` driver suffix that appears in `MODUS_DATABASE_URL`.

Backups written by `upgrade.sh` land in `deploy/backups/` (`.db` files for
SQLite, `.sql` files for PostgreSQL). The 10 most recent are kept.

## Federator (optional, experimental)

The federator is a separate service for the opt-in federation feature
(cross-deployment sharing of numeric benchmark deltas). A normal Modus
deployment does not use it, and federation is disabled by default
(`MODUS_FEDERATION_ENABLED=false`).

Two shapes are provided in `federator/` (copy `federator/.env.example` to
`.env` and set `FEDERATOR_JWT_SECRET` first):

```bash
cd federator

./deploy.sh --setup          # slim, static-first mode with Docker Compose (default)
./deploy.sh --setup --full   # full FastAPI stack
./deploy.sh --systemd        # install the systemd units in federator/systemd/ (bare metal, no Docker)
./deploy.sh --status         # check health
```

or run Compose directly: `docker compose -f docker-compose.slim.yml up -d`
(slim) or `docker compose up -d` (full). In slim mode Caddy serves pre-computed
static JSON and a Python write handler takes submissions.

Key settings (all read as `FEDERATOR_<NAME>`; see `federator/config.py`):
`JWT_SECRET` (required), `DATABASE_URL`, `PORT` (default 8443),
`RATE_LIMIT_SUBMITS_PER_HOUR`, `RATE_LIMIT_READS_PER_HOUR`,
`MIN_CONTRIBUTORS_FOR_RESULT` (default 3) and `CORS_ORIGINS`.

## Health checks

Orchestrator:

- `GET /health`: liveness
- `GET /ready`: readiness (includes a database check)
- `GET /metrics`: Prometheus metrics (`MODUS_METRICS_ENABLED`, on by default)
