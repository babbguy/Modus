# Modus

[![CI](https://github.com/babbguy/Modus/actions/workflows/ci.yml/badge.svg)](https://github.com/babbguy/Modus/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Modus is a self-hosted AI cost-governance platform. A small Python SDK
instruments your LLM client libraries, a FastAPI orchestrator evaluates budgets
and policies before each call and records usage, and a dependency-light web
dashboard shows spend by provider, model, team and app. It runs on
infrastructure you control and sends no usage data anywhere by default.

> **Status:** this is a portfolio project. It is not offered as a hosted
> service or a commercial product, and it is maintained on a best-effort basis.

## Features

- **Cost tracking** for Anthropic, OpenAI (including OpenAI-compatible
  endpoints such as xAI and Azure OpenAI), Google Gemini, AWS Bedrock, Groq,
  Mistral and Cohere, with per-app and per-team pricing overrides. Any other
  provider can be reported manually with `agent.record()`.
- **Pre-call enforcement.** Eleven policy types (budget caps, rate limits,
  model allow/deny lists, provider and environment blocks, token and latency
  caps, degradation ladders and more) can be created through the API or a
  policy file, and are evaluated before the provider call. A denied call
  raises `PolicyViolationError`, optionally with a cheaper suggested model.
- **Model routing.** Calibrated routing can send calls to cheaper models and
  escalate when output validation fails.
- **Insights.** Anomaly detection (requires PostgreSQL; skipped on SQLite),
  spend forecasting, scheduled finance reports, cost-center chargeback and
  agentic cost attribution.
- **Governance.** RBAC, SCIM provisioning, a hash-chained audit log with an
  offline verifier, notification channels (Slack, Teams, email, PagerDuty,
  webhooks) and an hourly governance loop that proposes policy changes for a
  human to approve.
- **Gateway proxy** (opt-in) for non-Python apps using OpenAI- or
  Anthropic-compatible clients.
- **Operations.** Prometheus metrics, SQLite or PostgreSQL storage, Alembic
  migrations, Docker Compose files and a Helm chart.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how these fit together.

### Experimental modules

The repository also contains exploratory modules that are not needed for the
core product and should not be treated as production security controls:
neuromorphic (spiking-network) enforcement simulation, post-quantum attestation
options, proof-style trajectory receipts (hash-chain commitments, not
zero-knowledge proofs), threshold-approval "swarm" governance, eBPF and CRDT
advisory budget tracking, genetic policy evolution, and opt-in federation. Each
module's docstring states its real scope.

## Repository layout

```
orchestrator/   FastAPI service: API, policy engine, ingest, routing, insights
sdk/            Python SDK (stdlib only): instruments provider clients in-process
dashboard/      Static single-page dashboard (vanilla JS, no build step)
conductor/      Optional org-wide aggregation service
federator/      Optional, experimental relay for opt-in benchmark sharing
plugins/        Drop-in Python plugins loaded at startup
migrations/     Alembic migrations (PostgreSQL)
deploy/         Compose, Helm chart, install/upgrade scripts
github-action/  CI actions (policy sync, PR cost estimate)
examples/       Example policy file and CI workflow template
docs/           Architecture, deployment, user and developer guides
tests/          Test suite
```

## Quickstart

### Option A: Docker Compose

Requires Docker with Compose v2. This builds the images from the local
Dockerfiles.

```bash
git clone https://github.com/babbguy/Modus.git
cd Modus
cp .env.example .env
```

Edit `.env`. For a throwaway local trial, set `MODUS_ENVIRONMENT=development`
and `MODUS_AUTH_MODE=stub` (stub auth gives every request admin rights and is
refused unless `MODUS_ENVIRONMENT=development`). Replace `MODUS_MASTER_API_KEY` with a random value, for
example `python -c "import secrets; print('mds_master_' + secrets.token_urlsafe(32))"`.
Then:

```bash
docker compose up --build        # orchestrator + PostgreSQL
# or, SQLite only, single container:
docker compose -f docker-compose.standalone.yml up --build
```

The dashboard and API are served at <http://localhost:8080>; interactive API
docs are at `/docs` outside production mode.

### Option B: Run from Python

Requires Python 3.10+ (3.12 recommended; CI and the Docker images use 3.12).
The pinned orchestrator dependencies do not install on 3.9. The SDK alone
declares `>=3.9`.

```bash
git clone https://github.com/babbguy/Modus.git
cd Modus
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r orchestrator/requirements.txt

export MODUS_ENVIRONMENT=development
export MODUS_AUTH_MODE=stub
export MODUS_DATABASE_URL=sqlite+aiosqlite:///modus.db
export MODUS_MASTER_API_KEY=mds_master_$(python -c "import secrets; print(secrets.token_urlsafe(32))")

python -m uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080
```

### Instrument an application

Install the SDK from your clone (a PyPI release is not published yet):

```bash
pip install ./sdk
export MODUS_URL=http://localhost:8080
```

The SDK registers itself using a team registration token. With the local
development setup above (stub auth), create a team and a token like this
(`MODUS_MASTER_API_KEY` is the value you set for the orchestrator):

```bash
curl -s -X POST "$MODUS_URL/api/v1/teams"   -H "Content-Type: application/json"   -d '{"slug": "demo", "name": "Demo"}'
# note the "id" in the response, then:
curl -s -X POST "$MODUS_URL/api/v1/teams/<id>/registration-token"   -H "X-Modus-APIKey: $MODUS_MASTER_API_KEY"
# copy "registration_token" (shown once) from the response:
export MODUS_TEAM_TOKEN=mds_team_...
```

With `MODUS_AUTH_MODE=jwt`, send the same master key (`-H "X-Modus-APIKey:
$MODUS_MASTER_API_KEY"`) or an admin bearer token when creating the team; see
[Authentication](docs/deployment.md#authentication). The master key is a root
credential, so keep it secret.


Then make `import modus` the first import of your entry point:

```python
import modus  # must come before any AI SDK import

import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
# Tokens, model, cost and latency are recorded automatically.
```

See [sdk/QUICKSTART.md](sdk/QUICKSTART.md) for the full walkthrough.

## Architecture in brief

```
Your app + SDK ──evaluate / ingest──▶ Orchestrator ──push (optional)──▶ Conductor
                                       │  FastAPI, SQLite/PostgreSQL
                                       └─ serves the dashboard
```

- **SDK**: standard library only (no third-party dependencies). Patches
  provider clients, asks the orchestrator for a decision before each call and
  flushes usage in the background.
- **Orchestrator**: the system of record. Uses FastAPI, SQLAlchemy, Pydantic
  and related libraries (see `orchestrator/requirements.txt`); it is not
  dependency-free.
- **Dashboard**: static files served by the orchestrator, with vendored
  Chart.js, its zoom plugin, Hammer.js, GridStack, Font Awesome and web fonts.
  No CDN calls.
- **Conductor**: optional service that aggregates several orchestrators.

## Data flow and privacy

Usage data stays in your database. By default Modus makes outbound requests
only to the LLM providers your own applications already call. Optional features
that contact other services are all off unless you configure them:

| Feature | Contacts | Notes |
|---------|----------|-------|
| Live price refresh | Provider model-list APIs | `MODUS_PRICING_LIVE_FETCH_ENABLED` (default `false`); bundled prices are used otherwise |
| Nomus integration | Your Nomus instance (`MODUS_NOMUS_URL`) | Pulls regulatory rules; sends no usage data. Inactive when unset |
| Dashboard assistant | The LLM provider you configure | Off until `MODUS_ASSISTANT_ENABLED` and a provider are set; sends only what you ask about |
| AI topology summaries | The LLM provider you configure | Disabled by default (`MODUS_SUMMARY_AGENT` empty) |
| AI insight text | `api.anthropic.com` | Off by default and does nothing unless you opt in twice: set the system setting `insights.anomaly.ai_explanations`, `insights.recommendations.ai_text` and/or `insights.efficiency.ai_text` to `true` (`PUT /api/v1/admin/settings/{key}` with body `{"value": "true"}`) **and** configure your own Anthropic key (`MODUS_ASSISTANT_PROVIDER=anthropic`, `MODUS_ASSISTANT_API_KEY`). The request then carries the app name and cost figures for that anomaly or recommendation. With either half missing, or if the call fails, a built-in template text is used and a warning is logged |
| Conductor push | Your Conductor (`MODUS_CONDUCTOR_URL`) | Must resolve to a private address |
| Federation | A federator you run | Opt-in, experimental, numeric deltas only |

## Configuration

The most common settings (see `.env.example` and
[docs/deployment.md](docs/deployment.md); every setting is a field of
`orchestrator/core/config.py`, prefixed with `MODUS_`):

```bash
MODUS_DATABASE_URL=sqlite+aiosqlite:///modus.db   # or postgresql+asyncpg://...
MODUS_MASTER_API_KEY=mds_master_<random>
MODUS_ENVIRONMENT=production                      # or development
MODUS_AUTH_MODE=jwt                               # stub is for local development only
MODUS_ENFORCEMENT_FAIL_OPEN=false                 # gateway and /api/v1/evaluate: deny (false) or allow (true) on timeout/error
MODUS_GATEWAY_ENABLED=false                       # enable the gateway proxy
MODUS_DISABLE_DASHBOARD=false                     # true if you serve the dashboard elsewhere
```

The SDK has its own switch, `MODUS_FAIL_OPEN` (default `true`), which decides
whether an application's AI calls proceed when the orchestrator cannot be
reached. Set it to `false` to deny calls instead.

## Development

```bash
pip install -r orchestrator/requirements.txt -r requirements-dev.txt
python -m pytest -q          # full suite (takes several minutes)
ruff check .                 # lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment guide](docs/deployment.md)
- [User guide](docs/USER_GUIDE.md)
- [Developer onboarding](docs/DEVELOPER_ONBOARDING.md)
- [Task guides](docs/guides/INDEX.md)
- [Benchmarking the gateway](docs/benchmarking.md)
- [SDK quick start](sdk/QUICKSTART.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)

## Related projects

[Nomus](https://github.com/babbguy/Nomus) is a sister project: a
regulatory applicability engine. Modus can optionally pull compiled
regulatory rules from a Nomus instance.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE)
for third-party attributions.
