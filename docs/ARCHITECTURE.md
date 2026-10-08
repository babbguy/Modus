# Modus Architecture

Modus is an AI cost-governance platform. It watches how applications spend on
LLM providers, enforces budgets and policies before each call is made, and
gives teams a dashboard for cost, usage and governance data. Everything runs on
infrastructure you control.

This document describes how the pieces fit together. For setup, see the
[README](../README.md) and the [deployment guide](deployment.md).

## Components

```
 Your application            Modus orchestrator                 Optional
 +---------------+  evaluate  +--------------------------+  push  +-----------+
 |  SDK (stdlib) |----------->| FastAPI + SQLAlchemy      |------->| Conductor |
 |  patches LLM  |  ingest    | policy engine, ingest,    |        | org-wide  |
 |  client libs  |----------->| routing, insights, API    |        | aggregate |
 +---------------+            | serves the dashboard      |        +-----------+
        ^                     +--------------------------+
        |  heartbeat (routing fingerprints)     ^
        +---------------------------------------+
 Non-Python apps --> Gateway proxy (/gateway/...) --> LLM provider
```

| Component | Path | Role |
|-----------|------|------|
| SDK | `sdk/` | Python package that instruments LLM client libraries in-process. Standard library only, no third-party dependencies. |
| Orchestrator | `orchestrator/` | FastAPI service backed by SQLite or PostgreSQL. Owns policies, usage ingest, budget counters, routing calibration, anomaly detection, notifications and the REST API. Serves the dashboard. |
| Dashboard | `dashboard/` | Static single-page app in vanilla JavaScript (no build step). Vendored Chart.js and GridStack for charts and tile layout. |
| Conductor | `conductor/` | Optional org-wide aggregation service. Orchestrators push aggregated data to it; it caches, reconciles and flags stale nodes so partial data is not presented as complete. |
| Federator | `federator/` | Optional, experimental relay for opt-in sharing of numeric benchmark deltas between deployments. Disabled by default. |
| Plugins | `plugins/` | Python files placed in this directory are loaded at startup (see `example_custom_policy.py`). |
| GitHub Action | `github-action/` | CI helpers: policy plan/apply and a PR cost-impact estimate. |
| Deploy | `deploy/`, `docker-compose*.yml` | Docker Compose files, a Helm chart and install/upgrade scripts. |

## Request flow

1. The SDK patches supported provider clients when `import modus` runs
   (Anthropic, OpenAI and OpenAI-compatible endpoints, Google Gemini, AWS
   Bedrock, Groq, Mistral, Cohere). Other providers can be reported manually
   with `agent.record(...)`.
2. Before each call the SDK asks `POST /api/v1/policy/evaluate` for a decision
   (`allow`, `deny` or `throttle`, optionally with a suggested cheaper model).
   Results are cached briefly. (`POST /api/v1/evaluate/` and its session
   endpoints are a second entry point for callers that are not using the SDK.) A `deny` or `throttle` raises
   `PolicyViolationError` in the application.
3. After the call the SDK buffers usage records and flushes them in batches to
   `POST /api/v1/ingest`. Ingest atomically updates real-time spend counters
   (hourly, daily and monthly windows) so budget checks do not wait for the
   periodic aggregation job.
4. Periodic heartbeats (`POST /api/v1/heartbeat`) return the app's routing
   fingerprints.
5. Background tasks aggregate usage, evaluate thresholds, run anomaly
   detectors, calibrate routing, sync prices and (optionally) push to a
   Conductor.

When the orchestrator is unreachable, the SDK behaviour is controlled by the
SDK-side `MODUS_FAIL_OPEN` environment variable (default `true`: calls proceed;
set `false` to deny them). The orchestrator-side `MODUS_ENFORCEMENT_FAIL_OPEN`
(default `false`) separately decides what the gateway and `POST /api/v1/evaluate/`
return when an evaluation times out or errors.

## Policy engine

Policies are evaluated in order of scope specificity (app, then team, then
platform) and then priority. The first `deny` or `throttle` wins. The engine
accepts eleven policy types through the API and policy files:

`model_allowlist`, `model_denylist`, `provider_block`, `environment_block`,
`token_cap`, `budget_cap`, `rate_limit`, `latency_cap`, `degradation_ladder`,
`amplification_gate` and `retry_circuit_breaker`.

The API (`VALID_POLICY_TYPES`), the policy engine and the SDK's
`policy_schema.POLICY_TYPES` all use this one list; a test fails if they drift.
There is deliberately no `webhook` policy type: an outbound call per evaluation
would need SSRF protection and an explicit opt-in, and none exists.

Apps also carry an enforcement state (`active`, `budget_suspended`,
`rate_limited`, `admin_suspended`) that short-circuits evaluation. Agent
sessions can have their own cumulative budget. Policies are created through the
API (`POST /api/v1/policies`); the dashboard lists them and shows enforcement
decisions. A policy-as-code format (`modus-policy.yaml`, validated by
`sdk/modus/policy_schema.py` and `python -m modus policy validate`) can be
applied with `python -m modus policy apply` (`POST /api/v1/policies/apply`),
which validates the whole file first and then creates, updates or deactivates
policies by name in one transaction.

## Teams

Teams (`orchestrator/api/teams.py`) group apps and carry the budgets, policies
and registration token for them. They can be nested (`parent_id`, cycles are
refused). `GET/PATCH/DELETE /api/v1/teams/{id}` and
`POST /api/v1/teams/{id}/restore` manage one team; deletion is soft
(`deleted_at`), keeps usage, cost and audit history, revokes the registration
token and refuses (409) while apps or child teams remain unless they are moved
(`reassign_to`, `reassign_children_to`) or the apps deactivated (`cascade`).
Moving or deactivating apps clears their entries from the in-process key cache,
so other orchestrator processes pick the change up after the app cache TTL
(five minutes).

## Cost tracking

Usage records carry provider, model, token counts, app, team and environment.
Costs are computed from a pricing table (`orchestrator/core/pricing.py`,
mirrored in `sdk/modus/pricing.py`) with per-app and per-team overrides.
Money is stored as `NUMERIC(18,8)` and handled with `Decimal`, never floats.
Prices can optionally be refreshed from provider APIs
(`MODUS_PRICING_LIVE_FETCH_ENABLED`, off by default).

## Model routing

The routing engine can steer calls to cheaper models when a call's structural
fingerprint matches a calibrated profile, and escalate when output validation
fails. A per-deployment calibrated table (built from observed traffic) is
authoritative. The SDK has a hook for a pre-trained generic classifier
(`routing_classifier.pkl`), but no such file is shipped in this repository, so
routing only starts once enough traffic has been observed and calibrated. A
drift monitor flags profiles whose behaviour changes. Routing is controlled by
`MODUS_ROUTING_ENABLED`.

## Insights and governance

* **Anomaly detection**: z-score scan over per-app daily aggregates (requires
  PostgreSQL; it is skipped on SQLite) plus pattern detectors for repeated
  threshold breaches, model-trigger patterns, team spend anomalies and
  enforcement hotspots.
* **Forecasting and reports**: spend forecasts, scheduled finance reports and
  cost-center chargeback.
* **Governance loop**: an hourly task that proposes policy changes with a
  rationale; a human applies or dismisses each proposal.
* **Audit log**: hash-chained, with signed checkpoints and an offline verifier
  (`scripts/verify_audit_export.py`).
* **Notifications**: Slack, Teams, email, PagerDuty and generic webhooks.
* **Access control**: users, roles, API keys, SCIM provisioning (`/api/v1/scim/v2`) and JWT auth.

## Gateway proxy

For applications that are not written in Python, the orchestrator can expose a
governed proxy under `/gateway/` for OpenAI-compatible and Anthropic
endpoints. Clients change their provider base URL and send an `X-Modus-APIKey`
header; evaluation and usage recording happen in the proxy. It is disabled by
default; enable it with `MODUS_GATEWAY_ENABLED=true`.

## Optional integration: Nomus

[Nomus](https://github.com/babbguy/Nomus) is a separate project, a
regulatory applicability engine. When `MODUS_NOMUS_URL` is configured, Modus
pulls signed, compiled regulatory rules from it and evaluates them locally.
Nothing about your usage is sent to Nomus. With no URL configured the
integration is inactive.

## Experimental modules

These modules are exploratory. Each states its real scope in its docstring, and
none of them is required for the core cost-governance features.

| Area | Modules | Scope |
|------|---------|-------|
| Neuromorphic enforcement | `neuromorphic_engine`, `snn_compiler`, `neuromorphic_hw`, `neuro_assurance` | A software-simulated spiking network; energy figures are illustrative constants. Hardware adapters are optional. |
| Proof-style receipts | `zk_circuit`, `zk_trajectory_prover`, `poe_ledger` | Trajectory "receipts" are SHA-256 hash-chain commitments, not zero-knowledge proofs. |
| Post-quantum | `pqc_signer`, `pqc_assessment`, `pqc_identity`, `pqc_migration` | Attestations use HMAC-SHA-512 by default; real ML-DSA signatures need the optional `pqcrypto` package. |
| Swarm governance | `mpc_engine` | k-of-n threshold approval, not privacy-preserving multi-party computation. |
| eBPF / CRDT | `ebpf_engine`, `crdt_sync` | Advisory budget tracking and a local cache; not hard enforcement. |
| Evolution | `constitutional_engine`, `policy_genome`, `grid_world` | Genetic search over policy parameters; proposals require human approval. |
| TRiSM sentinel | `trism_sentinel`, `trism_patterns` | Heuristic detectors for agentic-session threat patterns. |
| Federation | `federation_engine`, `federator/` | Opt-in sharing of numeric deltas with a k-anonymity threshold; not zero-knowledge or differentially private. |

## API conventions

The dashboard is a plain client of the public API; it reads the documented field
names and nothing else, so these conventions apply to every consumer.

* **Timestamps are timezone-aware UTC.** Every instant is ISO-8601 with an
  offset (`2026-10-08T14:10:54Z` or `+00:00`), on SQLite and PostgreSQL alike
  (the `UTCDateTime` column type normalises both). The only values without an
  offset are calendar *bucket labels* (`period` on cost-over-time series and the
  executive savings series), which name a day, not an instant.
* **Team scope.** Platform-admin identities (stub auth, the master key, platform
  admins) have no team of their own and see every team; most list endpoints take
  an optional `team_id` to narrow that. Any other identity sees only the teams in
  its `team_ids` (none means nothing), and naming a team outside that set is a 403.
* **Money** is a decimal string or a float rounded for display, never inferred
  from a float computation.

Response fields added or renamed for the dashboard:

| Endpoint | Fields |
|---|---|
| `GET /insights/anomalies`, `/insights/recommendations`, `/insights/enforcement-summary`, `/reports/roi` | `?team_id=` filter; admins no longer get empty results |
| `GET /insights/enforcement-summary` | `allowed` is derived from metered calls minus blocked and throttled (plain allow decisions are not stored) |
| `GET /insights/ops-kpis` (new) | `cost_this_hour`, `blocked_today`, `throttled_today`, `calls_today`, `tokens_per_call`, `avg_latency_ms`, `latency_samples`, `window_start` |
| `GET /reports/roi` | `net_savings`, `cost_per_blocked`, `roi_multiple` (null while `platform_cost_usd` is 0) |
| `GET /dashboard/top-models` | `pct`: share of all spend in the window and scope, not of the rows returned |
| `GET /governance/cot-ledger/verify` | without `team_id`, verifies every visible team's chain; adds `teams_checked` |
| `GET /governance/evolution/status` | `latest_population_size`, `total_mutations` (team-scoped) |
| `GET /sentinel/stats`, `/sentinel/threats` | stats add `by_action` and `blocked_threats`; threats add `app_id` |
| `GET /compliance/attestation-stats` | `merkle_roots` |
| `GET /compliance/pqc/score` | `pqc_algorithms` |
| `GET /compliance/zk-proofs/stats` | `coverage` (valid / total, 0-1) |
| `GET /apps`, `/apps/{id}` | `enforcement_state`, `enforcement_suspended_at`, `enforcement_suspended_reason` |
| `GET /finance/chargeback` | each row carries `period` (`YYYY-MM`) |
| `GET /finance/summary` | `cost_trend_pct` (month to date vs the same span of the prior month) |
| `GET /admin/nomus/status`, `POST /admin/nomus/sync` | `regulation_count` (distinct jurisdictions; `policy_count` is the rule count) |
| `GET /admin/federation/peers` | `team_slug` |
| `/audit-log` for `pricing_override` | `before` / `after` carry provider, model and both costs |

## Design constraints

* **Local by default.** Policy evaluation uses locally cached rules and the
  dashboard ships its own JS, CSS and fonts. Outbound calls are limited to the
  LLM providers you configure and integrations you switch on.
* **Small footprint.** No GPU requirement. SQLite works for a single node;
  PostgreSQL is supported for larger deployments.
* **Non-blocking.** The SDK flushes on a background thread, and the
  orchestrator is fully async.
* **Minimal SDK dependencies.** The SDK uses only the Python standard library.
  The orchestrator and conductor use FastAPI, SQLAlchemy, Pydantic and related
  libraries (see `orchestrator/requirements.txt`).
* **Careful money handling.** `Decimal` in Python and `NUMERIC(18,8)` in the
  database for monetary values; timestamps are UTC.

## Deployment shapes

| Mode | Components | Use case |
|------|-----------|----------|
| Standalone | Orchestrator + dashboard | One application or a small team |
| Org-wide | Several orchestrators + Conductor | Multiple applications or teams |

See [deployment.md](deployment.md) for Docker Compose, Python and Helm
instructions (systemd units exist only for the optional federator).
