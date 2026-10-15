# Modus

[![CI](https://github.com/babbguy/Modus/actions/workflows/ci.yml/badge.svg)](https://github.com/babbguy/Modus/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/babbguy/Modus)](https://github.com/babbguy/Modus/releases/latest)
[![License](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/modus-agent)](https://pypi.org/project/modus-agent/)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/babbguy/Modus/badge)](https://scorecard.dev/viewer/?uri=github.com/babbguy/Modus)

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
- **Insights.** Anomaly detection (SQLite and PostgreSQL),
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

You need Git and Docker with Compose v2. This starts a single container with
SQLite, built from the local Dockerfile (the first build takes a few minutes):

```bash
git clone https://github.com/babbguy/Modus.git
cd Modus
printf 'MODUS_ENVIRONMENT=development\nMODUS_AUTH_MODE=stub\nMODUS_MASTER_API_KEY=mds_master_%s\n' \
  "$(openssl rand -hex 24)" > .env
docker compose -f docker-compose.standalone.yml up --build -d
curl http://localhost:8080/health
```

The dashboard is at <http://localhost:8080> and the interactive API docs at
<http://localhost:8080/docs>. This is a local trial: stub auth gives every
request admin rights, and the orchestrator accepts it only with
`MODUS_ENVIRONMENT=development`. Stop it with
`docker compose -f docker-compose.standalone.yml down` (add `-v` to delete the
data). For a real deployment, start from `.env.example` and the
[deployment guide](docs/deployment.md).

### Other ways to run it

- **With PostgreSQL:** `cp .env.example .env`, set `POSTGRES_PASSWORD` and
  `MODUS_MASTER_API_KEY` (for a trial also `MODUS_ENVIRONMENT=development` and
  `MODUS_AUTH_MODE=stub`), then `docker compose up --build -d`.
- **From Python** (3.10+; CI and the images use 3.12):

  ```bash
  python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
  pip install -r orchestrator/requirements.txt
  export MODUS_ENVIRONMENT=development MODUS_AUTH_MODE=stub
  export MODUS_DATABASE_URL=sqlite+aiosqlite:///modus.db
  export MODUS_MASTER_API_KEY=mds_master_$(python -c "import secrets; print(secrets.token_urlsafe(32))")
  python -m uvicorn orchestrator.main:app --host 127.0.0.1 --port 8080
  ```

- **Published images, Helm, install scripts:** see the
  [deployment guide](docs/deployment.md). Release images are signed; see
  [Verifying releases](docs/verifying-releases.md).

### Instrument an application

Install the SDK, `modus-agent` (Python 3.10+, standard library only):

```bash
pip install modus-agent        # from PyPI, published from v1.2.0
# or, from the clone: pip install ./sdk
```

The SDK registers itself with a team registration token. Against the local
trial above (stub auth), create a team and a token:

```bash
export MODUS_URL=http://localhost:8080
set -a; . ./.env; set +a          # loads MODUS_MASTER_API_KEY

curl -s -X POST "$MODUS_URL/api/v1/teams" \
  -H "Content-Type: application/json" -d '{"slug": "demo", "name": "Demo"}'
# note the "id" in the response, then:
curl -s -X POST "$MODUS_URL/api/v1/teams/<id>/registration-token" \
  -H "X-Modus-APIKey: $MODUS_MASTER_API_KEY"
# copy "registration_token" (shown once) from the response:
export MODUS_TEAM_TOKEN=mds_team_...
```

With `MODUS_AUTH_MODE=jwt`, send the master key (`-H "X-Modus-APIKey:
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
- [Verifying releases](docs/verifying-releases.md)
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

Starting with v1.2.0, Modus is licensed under the
[Functional Source License, Version 1.1, ALv2 Future License](LICENSE)
(FSL-1.1-ALv2). Versions up to and including v1.1.0 remain under the
Apache License, Version 2.0.

You may use, copy, modify and redistribute Modus for any purpose other than a
Competing Use. The license defines a Competing Use as making the software
available to others in a commercial product or service that:

1. substitutes for the software;
2. substitutes for any other product or service we offer using the software
   that exists as of the date we make the software available; or
3. offers the same or substantially similar functionality as the software.

Internal use, non-commercial education, non-commercial research, and
professional services you provide to a licensee are expressly permitted. Each
version also becomes available under the Apache License, Version 2.0, on the
second anniversary of the date it is made available. The [LICENSE](LICENSE)
file is the full and binding text.

For a commercial license, open a discussion titled "Commercial license" in
this repository's [Discussions](https://github.com/babbguy/Modus/discussions).

See [NOTICE](NOTICE) for third-party attributions.
