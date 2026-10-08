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
- `GET /api/v1/insights/ops-kpis` (live cost this hour, blocked and throttled
  calls, tokens per call and mean latency for the DevOps view), plus new response
  fields listed under "API conventions" in `docs/ARCHITECTURE.md`.

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
- **SDK policy sync** called `POST /api/v1/policies` (the create endpoint), so
  agents never received their policies. It now calls `/api/v1/policies/sync`,
  keeps the last good set on failure (and logs a warning), and the sync
  response carries `scope` and `conditions`. Local enforcement now honours
  conditions, app/team/platform ordering and `warn` (which no longer skips the
  gateway check).
- **Alert delivery race**: notifications were started before the alert row was
  committed, so alerts stayed `notification_sent=false` and delivery rows could
  reference missing alerts. Alerts are now committed (per threshold) before
  delivery; `notification_sent`, `notification_result` and
  `notification_deliveries` reflect the real outcome; delivery tasks are held
  by a strong reference; transport errors are retried.
- **Notifications view** scored channel health by a field the writer never
  set. The delivery result is now `{status, success, error}`, defined once in
  `threshold_evaluator.channel_result` and read by the dashboard; alerts that
  were never dispatched are no longer counted as failures.
- **Connections** double-masked Slack, Teams and webhook URLs so Test hit a
  masked address and failed. Test now uses the stored secret and clients only
  ever see masked endpoints. The `degraded` state is now produced (slow, 5xx
  or elevated recent failure rate) and rendered, and the summary updates after
  a single-card test.
- **Degradation ladder** at 70% or more ended evaluation and skipped later
  policies (budget caps, denylists, rate limits). All policies are now
  evaluated and combined: deny, then throttle, then ladder downshift, then
  allow.
- **Alerts "New Rule" form** always failed (wrong scope and metric values, no
  team). It now posts a valid body, shows precise errors, and rules can be
  edited and deleted. The API rejects rules with a missing app or provider,
  a non-positive critical value, a warning value not below critical, or an app
  outside the team.
- Notification test endpoints reported "no channels configured" after a
  restart until the config page was opened; per-user and per-cost-center
  thresholds no longer fire against the whole team.

- Creating a team whose slug belonged to a soft-deleted team returned a 500;
  it now returns a 409 explaining the slug is reserved.
- The dashboard Create Team request dropped its authentication headers when the
  dashboard was signed in with a master key or JWT.
- Insights (anomalies, recommendations, enforcement summary) and the ROI report
  were empty for stub-mode, master-key and platform-admin identities because
  they filtered on `identity.team_id`, which is `None` for admins. Admins now see
  every team (optionally narrowed with `?team_id=`); other identities see only
  their own teams, and an identity with no teams sees nothing. The same
  "no team means everyone" fallback is removed from the apps list, top models,
  CoT ledger, Sentinel, attestation and ZK-proof statistics, and evolution status.
- Timestamps are timezone-aware UTC everywhere. SQLite returned naive datetimes
  that serialised without an offset, so browsers parsed them as local time and
  showed negative "ago" values. A `UTCDateTime` column type now normalises reads
  and writes on both databases, and the places that stringified timestamps by
  hand use `isoformat()`.
- Dashboard tiles that read fields the API never sent now read the documented
  names: Governance (CoT ledger `decision_summary` / `created_at` / `linked_*`,
  chain verification, evolution status), Sentinel (blocked and critical counts,
  threat action / confidence / time, ZK coverage), Compliance (Merkle roots, PQC
  algorithms), Finance (chargeback rollup and period, cost centers, create
  payload, spend trend), Pricing history, Executive (ROI, model risk share, team
  names, narrative), DevOps (anomalies, recommendations, enforcement, severity
  colours) and Federation. See "API conventions" in `docs/ARCHITECTURE.md`.
- The DevOps KPIs no longer invent numbers: "Cost This Session" (a fixed 4.8% of
  daily spend) and "P95 Latency" (a hard-coded 1,240 ms fallback) are replaced by
  "Cost This Hour" and "Avg Latency" from the new `GET /insights/ops-kpis`; the
  Executive ROI tile no longer assumes a $68 platform cost.
- The Apps view shows each app's enforcement state (active, budget suspended,
  rate limited, admin suspended) with the reason and time.
- The Nomus panel in Settings always showed 0 regulations: it read
  `regulations_count`, which the status endpoint never returned. The status and
  sync responses now carry `regulation_count`.
- Pricing-override audit entries recorded only the changed fields (updates) or
  only provider and model (deletes); they now carry the full before and after
  state so the history tile can show it.
- "Calls Allowed" on the DevOps enforcement tile was always 0 because allow
  decisions are not stored; it is now metered calls minus blocked and throttled.

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
