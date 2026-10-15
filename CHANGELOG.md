# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **License.** Starting with v1.2.0, Modus is licensed under the Functional
  Source License, Version 1.1, ALv2 Future License (FSL-1.1-ALv2) instead of
  the Apache License 2.0. You may use, copy, modify and redistribute it for any
  purpose other than a Competing Use, which the license defines as making the
  software available to others in a commercial product or service that
  substitutes for it, substitutes for any other product or service we offer
  using it that exists as of the date we make it available, or offers the same
  or substantially similar functionality (see `LICENSE` for the exact terms).
  Each version also becomes available under the Apache License 2.0 on the
  second anniversary of the date it is made available. Versions up to and
  including v1.1.0 remain under the Apache License 2.0. For a commercial
  license, open a discussion titled "Commercial license" in the repository's
  Discussions (https://github.com/babbguy/Modus/discussions). The SPDX
  identifier in the SDK package metadata, source file headers, the container
  image label and the dashboard Settings view is now `FSL-1.1-ALv2`, and the
  SDK wheel and sdist now include the license text (`sdk/LICENSE`).
- **Python dependencies upgraded** (`orchestrator/requirements.txt`):
  fastapi 0.135.3 → 0.136.3, uvicorn 0.44.0 → 0.54.0, uvloop 0.21.0 → 0.23.0,
  SQLAlchemy 2.0.49 → 2.0.54, asyncpg 0.30.0 → 0.31.0, aiosqlite 0.20.0 →
  0.22.1, alembic 1.14.0 → 1.20.0, psycopg2-binary 2.9.10 → 2.9.13, pydantic
  2.10.0 → 2.13.5, pydantic-settings 2.6.0 → 2.15.0, bcrypt 4.2.0 → 5.0.0,
  PyJWT 2.9.0 → 2.15.1, cryptography 44.0.0 → 50.0.2, prometheus-client
  0.21.0 → 0.26.0, httpx 0.28.0 → 0.28.1, aiosmtplib 3.0.2 → 5.1.3 and
  python-multipart 0.0.24 → 0.0.32. Development tools (`requirements-dev.txt`):
  pytest 9.1.1, pytest-asyncio 1.4.0, pytest-cov 7.1.0, ruff 0.17.0 and mypy
  2.4.0. SQLAlchemy stays on the 2.0 series because 2.1 requires Python 3.11
  and Modus supports Python 3.10. FastAPI stays below 0.137, which builds its
  route tables lazily on the event loop during the first requests to each
  router. Existing keys, tokens, encrypted credentials and audit checkpoints
  keep working unchanged. What operators may notice:
  - An API key, team registration token or invitation token longer than 72
    bytes is refused like any other wrong one (401 on ingest and
    self-registration, 404 on accepting an invitation). bcrypt 5 cannot check
    such input; Modus never issues one (its keys and tokens are 36 to 54
    bytes).
  - `MODUS_API_KEY_PREFIX` may be at most 40 bytes, so that an issued key fits
    in bcrypt's 72-byte limit. A longer prefix now stops the orchestrator at
    startup with a configuration error.
  - With `MODUS_AUTH_MODE=jwt`, a token whose `sub` claim is not a string is
    refused with 401 ("Token validation failed: Subject must be a string" in
    the log). With an HS256 secret, `MODUS_JWT_SECRET` should be at least 32
    bytes; a shorter one still works, but PyJWT writes an
    `InsecureKeyLengthWarning` to the server's error output.
  - `alembic.ini` uses `path_separator` instead of the deprecated
    `version_path_separator`.
- **GitHub Action and workflow templates upgraded and pinned.** The composite
  actions (`github-action/action.yml`, `github-action/cost-check/action.yml`)
  and the templates in `deploy/github-actions/workflows/` now use the same
  versions as this repository's workflows: setup-python 7 (was 5),
  github-script 9 (was 7), checkout 7 (was 4), docker setup-buildx 4 (was 3)
  and build-push 7 (was 5), each pinned to a full commit SHA with its version
  in a comment. They run on Node 24, so **self-hosted runners need GitHub
  Actions runner 2.327.1 or later** (GitHub-hosted runners already qualify).
  The shipped `github-script` steps need no changes for v9; a step of your own
  that calls `require('@actions/github')` or declares `getOctokit` with
  `const`/`let` does (see `deploy/github-actions/README.md`).

### Fixed

- **SDK route discovery on FastAPI 0.137 and later.** FastAPI 0.137 keeps
  each included router as a single entry in `app.routes` and builds its
  routes lazily, so the SDK's topology scan reported only the routes declared
  directly on the app and missed every route in an included router, AI
  endpoints included. Discovery now expands included routers, nested ones
  too, with their full prefixed paths, on every FastAPI version. The SDK
  still imports nothing outside the standard library.

- **CI actions upgraded and pinned.** Every action in `.github/workflows/` now
  runs its latest major (checkout 7, setup-python 7, setup-node 7,
  upload-artifact 7, github-script 9, docker setup-buildx 4, login 4,
  build-push 7, action-gh-release 3, gh-action-pypi-publish 1.14) and is
  pinned to a full commit SHA with the version in a comment. They all run on
  Node 24, so the reusable `modus-policy-sync.yml` workflow needs GitHub
  Actions runner 2.327.1 or later on self-hosted runners (GitHub-hosted
  runners already qualify). Its pull-request plan comment now waits for the
  API call, so a failed comment fails the step instead of passing silently.
  `publish.yml` gains a `dry_run` input for manual runs that builds both
  images without pushing them; tag pushes publish as before.

- **Dependabot.** Python version updates now also cover `conductor/` and
  `federator/`, in the same monthly group as the orchestrator. Two upgrades
  are held back by documented ignore rules, each with the condition for
  revisiting it: FastAPI 0.137+ (lazy route tables built on the event loop)
  and SQLAlchemy 2.1 (needs Python 3.11; Modus supports 3.10). The rules
  filter version updates only, not security updates.

- **Conductor and Federator dependencies.** The Conductor's pins move to the
  orchestrator's versions (FastAPI 0.136.3, Starlette 1.8, PyJWT 2.15.1,
  cryptography 50.0.2, SQLAlchemy 2.0.54, pydantic 2.13.5 and the rest),
  which clears the 36 known advisories in its old set, and drops five
  packages it never imports (alembic, psycopg2-binary, bcrypt,
  prometheus-client, python-multipart). The Federator's open version ranges
  become exact pins at the same versions, so its images no longer pick up
  whatever is newest at build time. In the Conductor's JWT mode, a token
  whose `sub` claim is not a string is now refused (401), as on the
  orchestrator.

- **Conductor in `deploy/docker-compose.prod.yml` (org-wide mode).** The
  Conductor reads `CONDUCTOR_*` variables, but the Compose file passed it
  `MODUS_DATABASE_URL` and `MODUS_PORT`, which it ignored. In production it
  then saw no push secret and stub auth and refused to start, so
  `install.sh --org-wide` left it restarting; with those set, its database
  went to `/app/conductor.db` in the container's writable layer instead of
  the `conductor_data` volume, and was lost when the container was
  recreated. The Compose file now maps the shared `.env` onto the
  Conductor's own variables: `MODUS_CONDUCTOR_DATABASE_URL` (default: SQLite
  on the volume), `MODUS_ENVIRONMENT`, `MODUS_AUTH_MODE`, `MODUS_JWT_SECRET`
  and `MODUS_CONDUCTOR_SECRET`, the secret Orchestrators push with.
  `install.sh --org-wide` generates `MODUS_CONDUCTOR_SECRET` when it is not
  set. Any other `CONDUCTOR_*` setting can go in `deploy/.env` as-is. The
  image's database also defaults to the `/app/data` volume.
  `CONDUCTOR_DATABASE_URL` works as before.
- **Conductor health check.** The image and the Compose file probed
  `/health`, which the Conductor does not serve, so the container was always
  `unhealthy`. They now probe its liveness route, `/healthz`.
- **Federator images.** Both failed with their own Compose files. The full
  image copied the files to `/app` without the `federator` package directory,
  so `uvicorn federator.main:app` failed with `ModuleNotFoundError`; the slim
  image's `COPY federator/` did not exist in its build context. Both now copy
  the package to `federator/`, and a `.dockerignore` keeps `.env` (which the
  full image used to include, `FEDERATOR_JWT_SECRET` and all) and the
  deployment files out. In slim mode:
  - the aggregator imported `federator.config`, which needs pydantic, absent
    from the slim image, so it never wrote the static files Caddy serves;
  - Caddy proxied writes to `localhost:8444`, its own container, instead of
    the writer;
  - per-industry results (`/v1/results/<industry>`) were always 404, because
    of a wrong rewrite in the `Caddyfile`;
  - the writer refused every token carrying the issuer and audience the full
    service requires. It now checks `FEDERATOR_JWT_ISSUER` and
    `FEDERATOR_JWT_AUDIENCE` (same defaults as the full service), so tokens
    without them are refused, as in full mode.

  The full Compose file now reads `federator/.env` (documented settings such
  as `FEDERATOR_CORS_ORIGINS` never reached the service) and its Caddy proxies
  to the federator (`Caddyfile.full`) instead of using the slim-mode routes.
  Both Compose files take the domain from `FEDERATOR_DOMAIN` in `.env`
  instead of an edit to the `Caddyfile`.
- **Comma-separated CORS origins.** `CONDUCTOR_CORS_ORIGINS` and
  `FEDERATOR_CORS_ORIGINS` set to a comma-separated list, a single origin,
  `*` or an empty value stopped the service at startup with a
  `SettingsError`: only a JSON list worked. They are now parsed as the
  orchestrator parses `MODUS_CORS_ORIGINS`, and a JSON list still works.
- **Federator bare-metal install (`deploy.sh --systemd`).** It never
  completed: it created the virtualenv as the `federator` user in a
  root-owned `/opt/federator` (permission denied), then copied a
  `federator/` directory that does not exist where the script runs. The
  writer unit could not have run either: it passed uvicorn `--fd 0`, which is
  stdin, not the socket systemd hands over (fd 3), and it was `Type=notify`
  with a watchdog, which uvicorn never signals, so systemd killed it after
  the start timeout. The script now installs the package without secrets or
  deployment files, a root-owned virtualenv, `.env` readable only by the
  service and the units, then starts the writer and publishes the first
  static files; re-running it updates an install and keeps the data. The
  writer unit is `Type=exec` with `--fd 3` and `Restart=on-failure`, and
  requires `/opt/federator/.env`. The slim writer now refuses to start
  without `FEDERATOR_JWT_SECRET`, as the full service does, instead of
  starting and answering every submission with 401.
- **Federator `deploy.sh --status`.** It requested `http://localhost:443`,
  plain HTTP on Caddy's TLS port, which always fails, and reported nothing
  when no check passed. It now checks the service on the address it listens
  on and `https://<FEDERATOR_DOMAIN>/health` through Caddy, reports each
  result, and exits non-zero if a check fails. `--status --systemd` checks a
  bare-metal install. `--setup` waits for the same checks.
- **Conductor readiness.** `GET /ready` answered 200 with
  `"status": "not_ready"` when the database was down, so a load balancer or
  readiness probe kept sending traffic to it, and 500 before the database was
  initialised. It now answers 503, as the orchestrator does, and logs the
  cause. The Compose and image health checks probe `/healthz` and are
  unchanged, so a database outage does not restart the container.
- **Reported version.** Under `deploy/`, the orchestrator's `/health`,
  `/ready` and `/version` reported `"version": "latest"`: `MODUS_VERSION` in
  `.env` is the image tag, and the Compose file passes `.env` to the
  container, where it also set the orchestrator's `version` setting. The
  Conductor reported `1.0.0` whatever it was. The version is no longer a
  setting: the Dockerfiles take a `MODUS_BUILD_VERSION` build argument,
  check that it is a version, write it into the image and set the
  `org.opencontainers.image.version` label, and the services report it. An
  image built without it (such as `docker compose up --build`) reports the
  version of its source. `publish.yml` passes the release version and fails
  a release whose version differs from the source's, and CI checks that the
  orchestrator, Conductor and SDK versions agree (`scripts/check_versions.py`).
  `MODUS_VERSION` still selects the image tag.
- **Remaining customer action templates.** `deploy-orchestrator.yml` and
  `register-app.yml` still used tag-pinned actions on old runtimes, two of
  which no longer run on current GitHub runners: azure/setup-helm v3 (Node
  16) and gliech/create-github-secret-action v1 (Node 12). Now
  aws-actions/configure-aws-credentials 6.3.0 (was v4, Node 20),
  aws-actions/amazon-ecr-login 2.1.7 (still the latest major),
  docker/metadata-action 6.2.0 (was v5, Node 20) and azure/setup-helm 5.0.1,
  each pinned to a full commit SHA with its version in a comment; every
  step's inputs exist in the pinned version. gliech/create-github-secret-action
  has had no release since 2022 and has no Node 20 or 24 version, so
  `register-app.yml` now stores `MODUS_API_KEY` and `MODUS_APP_ID` with
  `gh secret set`, preinstalled on GitHub-hosted runners, using the same
  `GH_PAT_SECRETS_WRITE` token and the same repository, with the values on
  stdin. The production smoke test in `deploy-orchestrator.yml` expected
  `/ready` to answer `"status": "ready"`, but the orchestrator answers `"ok"`,
  so every production deploy failed its smoke test; it now checks for `"ok"`.

## [1.1.0] - 2026-10-09

### Added

- **Release gate** (`e2e/`, workflow `release-gate.yml`, required checks
  `Release gate (sqlite)` and `Release gate (postgres)`): runs the production
  image capped at 1 CPU and 1 GiB, on SQLite and on PostgreSQL 16, drives it
  with the repository's own SDK and a real browser, and fails on stale reads
  after a write, usage that does not reconcile exactly (8 decimal places)
  across the overview, cost-over-time, by-app, top-models, finance and session
  figures, broken enforcement (budget cap, rate-limit throttle, model
  denylist, degradation ladder, evaluate timeout), undelivered alerts, wrong
  connection health, any 429 or 5xx, ERROR lines or tracebacks in the server
  log, dashboard console errors, failed API calls or empty tiles, peak memory
  above 512 MiB and idle CPU above 5 %. One command runs it locally:
  `python e2e/run_gate.py --db sqlite|postgres`. See `e2e/README.md`.
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
