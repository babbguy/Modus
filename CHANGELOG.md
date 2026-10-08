# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Team management**: `GET /api/v1/teams/{id}` (budgets, parent, child teams,
  app / member / policy counts), `PATCH /api/v1/teams/{id}` (name, slug,
  description, department, parent, budgets, budget period, cost center),
  `DELETE /api/v1/teams/{id}` (soft delete with `reassign_to`, `cascade` and
  `reassign_children_to`; refuses with 409 while apps or child teams remain;
  always revokes the registration token; usage history is kept),
  `POST /api/v1/teams/{id}/restore` and `GET /api/v1/teams?include_deleted=true`.
  Every change is audit-logged with before and after values. The dashboard
  Teams view gains Edit, Delete (with app reassignment) and Restore.

### Changed

- **Usage aggregation is counted once, at ingest.** The writer adds each
  batch's usage to its hourly and daily aggregate rows in the same transaction
  that claims the batch id; the aggregation task now only reconciles daily rows
  against hourly rows (replace on mismatch) and retention only deletes detail.
  See docs/ARCHITECTURE.md "Usage data pipeline". Aggregates written by earlier
  versions may already be inflated; they are not rewritten.
- **API rate limits have two classes**: SDK machine traffic with an app key
  (`MODUS_RATE_LIMIT_SDK_PER_MINUTE=6000`, `MODUS_RATE_LIMIT_SDK_BURST=1000`)
  and everything else (`MODUS_RATE_LIMIT_PER_MINUTE=200`,
  `MODUS_RATE_LIMIT_BURST=50`). `429` responses carry `Retry-After`.
- SDK: ingest payloads are retried with the same `batch_id` until accepted
  (bounded by `MODUS_MAX_PENDING_BATCHES`), raw payloads are split into chunks
  of 500 records, and aggregation buckets are per UTC hour.
- New setting `MODUS_HOURLY_AGGREGATE_RETENTION_DAYS` (default 7).

### Fixed

- The aggregator re-read the last two days of raw usage every minute and added
  it to the aggregates again (prior-day totals ~20x too high), storage
  compaction re-added compacted records, and every aggregation cycle also
  inflated the real-time spend counters used for budget enforcement.
- Dashboard KPIs read only daily rows while SDK ingest wrote only hourly rows,
  so live SDK spend did not appear until the faulty rollup ran. Overview,
  finance, insights and reports now read current daily rows; overview MTD
  equals finance MTD.
- Retried aggregated SDK batches were counted again (no batch-id dedup on the
  aggregated path), and the SDK re-sent failed flushes under a new batch id.
- SDK sampled traces were counted on top of the aggregates by the old rollup;
  policy-violation and error calls were left out of the aggregates.
- `/ingest` dropped the session id, so SDK sessions never reached the Sessions
  view or the attribution engine. Raw records and aggregated-mode traces now
  carry `mds_session_id` into `usage_records.session_id`; `agent.record()`
  inside `agent.session()` attaches the session and span ids; aggregated mode
  sends session calls as traces. Session ids are validated (1-64 characters).
- The attribution engine stopped picking up new sessions once 20 sessions had
  been processed.
- "vs prior 7d" compared the last 7 days with a 14-day total (always about
  -50%); it now compares with the 7 days before.
- Anomaly detection and spend forecasts never ran on SQLite (the default); both
  are now portable. The anomaly rate now uses the last 1-2 hours of usage
  instead of mixing one hour of spend with the hours elapsed today.
- The team spend-anomaly detector summed hourly and daily rows together.
- The API rate limit (~250 requests/min per key) throttled normal SDK traffic,
  and the SDK treated `429` as "orchestrator unreachable": it failed open and
  counted toward its circuit breaker. `evaluate` now retries with jitter and
  keeps enforcing the last server decision; ingest backs off and retries the
  same batch.
- `GET /api/v1/dashboard/summary` returned 500 on PostgreSQL once usage
  existed (`SUM(bigint)` comes back as Decimal and was divided by a float).
- `/ingest` answered `202` even when the write queue was full and the batch
  was dropped; it now answers `503` with `Retry-After`.
- Creating a team whose slug belonged to a soft-deleted team returned a 500;
  it now returns a 409 explaining the slug is reserved.
- The dashboard Create Team request dropped its authentication headers when the
  dashboard was signed in with a master key or JWT.

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
