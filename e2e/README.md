# Release gate

The release gate runs the real product the way users do and fails on the bugs
that in-process unit tests cannot see: responses sent before their transaction
commits, event-loop stalls, usage counted twice, dashboard and API fields that
do not match, and timeouts on a small server. It is a required CI check
(`Release gate (sqlite)` and `Release gate (postgres)`).

## What it proves

One run starts the production image built from the repository's `Dockerfile`,
capped at `--cpus=1 --memory=1g --memory-swap=1g`, in production mode with JWT
auth and a freshly generated master key, on SQLite or on PostgreSQL 16 (its own
container, schema applied by the image entrypoint's Alembic step). Then:

| Phase | What must hold |
|---|---|
| write-then-read | 12 rounds of create-then-use on the next request: team then read it back, team then registration token, token then self-register then evaluate with the new key, deny policy then evaluate (must deny), admin app registration then evaluate and list. Any 404, 401 or stale allow fails. |
| traffic | Real `ModusAgent` clients from `sdk/`: 3 apps plus a burst app register at the same instant, send sessions, spans and plain calls for 45 s, then 600 governed calls in a burst. Zero registration failures, zero 429s, zero circuit-breaker trips, nothing left unsent; a denied model is refused every time by the SDK's synced local policy. |
| reconcile | Every figure (overview today/7d/30d/MTD cost and calls, cost-over-time daily and hourly, by-app, top models, finance, attribution sessions) equals what was sent, exactly at 8 decimal places (finance reports cents). Checked right after the traffic, after at least 4 aggregator cycles, and at the end. |
| scenarios | Budget cap deny; rate-limit policy throttle with `retry_after_seconds`; model denylist enforced by the server for a policy the SDK has not synced; degradation-ladder downshift that never lets a denied model through; alert threshold firing, delivered to a local webhook receiver and recorded as delivered; connection health reported as degraded (slow, 503) and error (unreachable); `/api/v1/evaluate/` under an external database lock returns the fail-closed deny, never a 5xx. |
| browser | Playwright signs in with the master key and opens all 22 dashboard views after the traffic. Fails on console errors, failed API calls, `NaN` / `undefined` / `[object Object]` / `Invalid Date` / negative "ago" text, tiles stuck loading or showing an error, tiles that must have data but are empty (`sweep/view_expectations.json`), and overview KPIs that differ from the traffic sent. One screenshot per view. |
| resources | Peak memory at most 512 MiB (`docker stats`); idle CPU at most 5 % of one core, averaged over 60 s after a 15 s warm-up from the container's cgroup CPU counter; p50/p95/p99 of `/api/v1/policy/evaluate` at concurrency 10 reported in the summary. |
| server-logs | No ERROR/CRITICAL line, traceback, unhandled exception, `PendingRollbackError`, 5xx or 429 anywhere in the run. The allow-list in `gate/logscan.py` is empty; any entry added must say why it is expected. |

The gate's own admin traffic is paced under the default API rate limit (200
per minute) instead of raising it, so every 429 it sees is a real finding.

## Run it locally

Requirements: Docker (Docker Desktop on Windows or macOS), Python 3.10+, Node 20+.

```bash
cd e2e && npm ci && npx playwright install chromium && cd ..
python e2e/run_gate.py --db sqlite                 # builds the Dockerfile as modus:gate
python e2e/run_gate.py --db postgres --image modus:gate
```

Options: `--image TAG` tests an existing image; `--sdk DIR` drives traffic with
another SDK checkout (default: this repository's `sdk/`); `--out DIR` sets the
artifact directory (default `e2e/out/<db>`); `--chromium PATH` uses a specific
browser. `--skip-browser` and `--keep` are for debugging only.

The command prints a PASS/FAIL table and exits non-zero on any failure. The
artifact directory holds `summary.md`, `gate-results.json`, the container logs,
`docker-stats.csv`, `sdk.log` (SDK warnings), `sweep-results.json` and
`screenshots/`. A full run takes
about 6 minutes after the image is built. Every container and network it
starts (`modus-gate-<id>-*`) is removed at the end.

## Changing the expectations

`sweep/view_expectations.json` lists, per dashboard view, the tiles
(`tilesWithData`), row selectors (`rowsWithData`) and text (`textIncludes`)
that the seeded traffic guarantees, plus API errors the view triggers on
purpose (`allowedApiErrors`). A view added to the sidebar must be added here,
or the gate fails. Do not loosen an assertion to make the gate pass; fix the
product, or explain in the pull request why the expectation was wrong.
