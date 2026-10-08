# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-10-07 - Initial public release

First public release, under the Apache License 2.0.

### Included

- **SDK** (`sdk/`, standard library only): in-process instrumentation of
  Anthropic, OpenAI (and OpenAI-compatible endpoints), Google Gemini, AWS
  Bedrock, Groq, Mistral and Cohere clients; pre-call policy evaluation;
  buffered usage reporting; optional response cache, output validator, rewind
  hooks and OpenTelemetry exporter.
- **Orchestrator** (`orchestrator/`): FastAPI service on SQLite or PostgreSQL
  with usage ingest, real-time spend counters, eleven policy types, enforcement
  states and session budgets, per-app and per-team pricing overrides,
  thresholds and alerts, anomaly detection (PostgreSQL), forecasting,
  scheduled finance reports, cost attribution, notification channels, RBAC and SCIM, a
  hash-chained audit log with signed checkpoints and an offline verifier, and
  Prometheus metrics.
- **Model routing**: calibrated routing with escalation and drift monitoring.
- **Gateway proxy** (opt-in): governed proxy for OpenAI- and
  Anthropic-compatible clients, with per-provider circuit breaking and
  optional mid-stream budget enforcement when a stream budget limit is set.
- **Dashboard** (`dashboard/`): static single-page app with tile layouts for
  overview, DevOps, executive, finance, governance, policies, routing, alerts,
  teams, apps, topology, pricing and settings views. All assets are bundled.
- **Conductor** (`conductor/`): optional org-wide aggregation with caching and
  reconciliation.
- **Deployment**: Dockerfiles, Compose files, a Helm chart, systemd units for
  the optional federator, Alembic migrations (a baseline plus the billing-actuals natural key), and install,
  upgrade and uninstall scripts.
- **CI integrations**: GitHub Actions for policy sync and a PR cost-impact
  estimate.
- **Optional Nomus integration**: pulls signed regulatory rules from a Nomus
  instance when `MODUS_NOMUS_URL` is configured; inactive otherwise.
- **Experimental modules**: neuromorphic enforcement simulation, post-quantum
  attestation options, hash-chain trajectory receipts, threshold approval,
  eBPF and CRDT advisory budget tracking, genetic policy evolution, TRiSM
  sentinel and opt-in federation (with the `federator/` relay). Their
  docstrings describe their actual scope.
- Container images are built from the repository; none are published until a
  `v*` tag is released. The SDK is installed from the repository, not PyPI.

### Behaviour worth knowing

- **Master key as an admin credential.** `X-Modus-APIKey: mds_master_...` is
  accepted by every admin endpoint in both `stub` and `jwt` auth modes (audit
  actor `master-key`). The policy CLI, GitHub Action, seed script and dashboard
  sign-in use it. Treat it like a root password.
- **No outbound call by default.** AI-written insight text is off; it needs the
  `insights.*.ai_*` system setting set to `true` and your own Anthropic key
  (`MODUS_ASSISTANT_PROVIDER=anthropic`, `MODUS_ASSISTANT_API_KEY`).
- **Policy-as-code works.** `POST /api/v1/policies/apply` validates the whole
  file, resolves teams by slug/name/UUID, and creates, updates or deactivates
  policies atomically. Eleven policy types, one list shared by the API, engine
  and SDK (there is no `webhook` type).
- **Reconciliation.** Import provider invoice totals as JSON or CSV
  (`/api/v1/finance/reconciliation/import[/csv]`); Modus does not pull invoices
  from providers. Tracked cost and variance are recomputed on read.
- **Dashboard.** Sign in with the master key or a JWT (kept in sessionStorage,
  never accepted from the URL); the New Policy form matches the API; Settings,
  Auto-refresh re-fetches the current view; Admin and Federation use the real
  endpoints and show errors instead of demo data.
- **Helm chart.** No phantom dashboard/relay objects; `MODUS_JWT_SECRET`,
  master key and database URL come from secrets (existing or chart-created);
  optional `NetworkPolicy`.
- **PostgreSQL.** Raw-SQL finance/insights queries bind timestamps correctly
  (they returned HTTP 500 before); SQLite month-to-date totals no longer drop
  the first day of the month.
