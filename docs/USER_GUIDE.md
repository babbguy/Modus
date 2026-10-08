# Modus — End User Guide

> The complete reference for using Modus to govern, monitor, and optimize
> your organization's AI spending. Written for FinOps leads, DevOps engineers,
> engineering managers, and executives — not platform developers.
>
> **Audience:** End users

---

## Table of Contents

1. [Welcome & First 5 Minutes](#1-welcome--first-5-minutes)
2. [The Dashboard at a Glance](#2-the-dashboard-at-a-glance)
3. [Understanding Each View](#3-understanding-each-view)
   - 3.1 [Overview](#31-overview)
   - 3.2 [DevOps](#32-devops)
   - 3.3 [Finance](#33-finance)
   - 3.4 [Policies](#34-policies)
   - 3.5 [Routing](#35-routing)
   - 3.6 [Sessions](#36-sessions)
   - 3.7 [Pricing](#37-pricing)
   - 3.8 [Notifications](#38-notifications)
   - 3.9 [Topology](#39-topology)
   - 3.10 [Governance](#310-governance)
   - 3.11 [Teams](#311-teams)
4. [Common Tasks](#4-common-tasks)
5. [Glossary of Metrics](#5-glossary-of-metrics)
6. [Troubleshooting](#6-troubleshooting)
7. [FAQ](#7-faq)
8. [Where to Get Help](#8-where-to-get-help)

---

## 1. Welcome & First 5 Minutes

Modus is a self-hosted AI cost governance platform. It watches the AI calls
your instrumented applications make, enforces budgets and policies **before**
the call happens, can route calls to cheaper models when quality allows, and
shows you where your spend is going. Usage data stays in your own database.

### What you can do with Modus

- **See every AI dollar spent** across all apps, teams, models, and providers
- **Stop overspend** with budgets that block calls before they incur cost
- **Catch anomalies** before they show up on the next bill
- **Reduce spend** by routing eligible traffic to cheaper models automatically
- **Allocate cost** to cost centers for chargeback and accounting
- **Predict end-of-month spend** before the month ends

### Your first 5 minutes

1. Open the dashboard at the URL your administrator gave you (a local
   install serves it at `http://localhost:8080/`). If the orchestrator uses
   `jwt` auth, paste the master key (`mds_master_...`) or the bearer token (JWT)
   you were given into the sign-in screen; it is kept for that browser tab only.
2. Land on the **Overview** view — this is the home page.
3. Look at the top KPI row: today's cost, 7-day cost, monthly budget burn,
   and risk status. If budget burn is green, you're on track.
4. Scroll down to **Top Apps by Cost**. Click any row to drill into that
   app's model breakdown and recent alerts.
5. Click the **Cost Over Time** chart at any peak — a modal will show what
   apps and models drove that period's spend.

That's the loop: **observe → drill → act**. Everything else is depth.

---

## 2. The Dashboard at a Glance

The Modus dashboard has three layers:

| Layer | Purpose | Updates |
|---|---|---|
| **KPI rows** | At-a-glance health | On load and view switch |
| **Tiles (charts/tables)** | Filterable detail | On load and view switch |
| **Modals (drill-throughs)** | Deep investigation | On click |

Live views re-fetch their data on the interval set under Settings,
Auto-refresh (30 seconds by default, or Off), skipping hidden tabs. Between
refreshes, API responses are cached in the browser for 30 seconds (stale data
is shown while fresh data loads).

### Navigation

The left sidebar lists every view. Use the period selector at the top to
control the time range for cost-over-time charts and breakdown tables.
**The Finance view always shows month-to-date** — it doesn't change with
the period selector.

### Hover for help

Tiles that have registered help text show a `?` icon next to the title. Hover
for a short explanation; "Learn more" in the tooltip jumps to the matching
guide in the Help view.

---

## 3. Understanding Each View

### 3.1 Overview

The home page. Designed to answer: **"Is anything on fire right now?"**

**KPI row** (top): Cost today, 7d, 30d; tokens, calls, active apps, online
agents; budget burn %, status, teams at risk.

**Cost Over Time chart**: Trend of total cost across the selected period.
**Click any data point** to open a modal showing the apps, providers, and
models that drove that period's spend.

**Top Apps by Cost**: Your most expensive apps. **Click any row** to open a
modal with the app's model breakdown, recent alerts, and metadata.

**Top Models**: Models ranked by cost. Useful for spotting "I didn't realize
GPT-4 cost so much" surprises.

**Recent Alerts**: Last 20 fired alerts. Click any alert for full details.

**Provider Breakdown**: Cost split across Anthropic, OpenAI, Bedrock, etc.

**Agent Status**: Which apps are reporting telemetry. Offline = the app's
agent SDK isn't sending data, possibly because the app is down or the API
key is invalid.

### 3.2 DevOps

Designed for engineers responsible for AI service health.

**KPI row**: Cost this session, cost today, blocked calls today, tokens per
call, p95 latency, online agents.

**Anomaly Detection**: AI cost or behavior that deviates from a 14-day
rolling baseline. Each anomaly shows a z-score (how many standard deviations
from normal) and an explanation (a template text unless an AI explanation was
generated; see the README's data-flow table). **Click an anomaly** for details.
The z-score scan needs PostgreSQL and does not run on SQLite.

**Model Optimization**: Recommendations to swap to cheaper models where
quality allows. Estimated monthly savings shown.

**Enforcement**: Allow/block/throttle/redirect counts plus cost saved by
blocks today.

**Cost by Deployment**: Per git-SHA cost so you can spot which deploy
changed your AI spend.

**Provider Breakdown**: Same as Overview but DevOps-scoped.

**Agent Registry**: Every registered app, its environment, last-seen time,
and instrumented providers.

### 3.3 Finance

Designed for FinOps leads and finance teams. Always shows month-to-date.

**KPI row**: MTD spend, end-of-month forecast, budget remaining, overall
status (on-track / at-risk / over-budget).

**Spend Trend (MTD)**: Daily spend with the daily budget line overlaid.
Toggle **Compare** to see prior month overlaid as a dashed line.

**Spend by Department**: Donut chart split by cost center department.
Empty until you assign teams to cost centers.

**Budget Burn Rate by Team**: Each team's budget, current spend, projected
end-of-month, burn %, and risk classification. **Toggle Compare** to add a
"Δ vs Prior" column.

**Spend Forecast**: Statistical projection for end-of-month, end-of-quarter,
and end-of-year. Shows R² (forecast confidence) and the method used.

**Budget Breach Alerts**: Teams predicted to exceed budget, with the
predicted breach date.

**Team Budget Utilization**: Visual gauges per team.

**Chargeback**: Generated chargeback allocations per cost center.
Click "Generate" to compute fresh allocations from current usage.

**Reconciliation Status**: Tracked vs billed cost variance per imported
provider invoice total. Use **Import CSV** (or `POST
/api/v1/finance/reconciliation/import`) to load invoice totals; the tile shows
"No invoice totals imported" until you do. Modus does not pull invoices from
providers. See the chargeback guide for the file format.

**Cost Center Assignment**: The cost center registry. Click "+ Assign" to
create a new cost center. Teams are linked to cost centers through
`POST /api/v1/finance/allocation` (there is no dashboard form for that yet).

**Scheduled Reports**: Chargeback, burn-rate, variance, audit-trail and
forecast reports on a daily, weekly, monthly or quarterly schedule, delivered
to a webhook or Slack. Create them with `POST /api/v1/finance/reports`; the
tile is a read-only list (name, type, frequency, channel, last run, status).

### 3.4 Policies

The governance rule list. Each policy is one of: budget cap, rate limit,
model allowlist, model denylist, provider block, environment block, token cap,
latency cap, degradation ladder, amplification gate or retry circuit breaker.
Create policies through the API (`POST /api/v1/policies`); see
[guide 01](guides/01-set-your-first-budget.md).

**Governance Policies** table: every active and inactive policy.

**Enforcement Detail**: top denial reasons, decision counts, and the
25 most recent allow/deny/throttle decisions with policy name, action,
provider, model, estimated cost, and reason. This is where you confirm
"is my policy actually doing what I think it's doing?"

### 3.5 Routing

Modus's intelligent model routing. Routes eligible traffic from
expensive models to cheaper ones when quality is maintained.

- **Savings Over Time**: Cumulative savings from routing decisions.
- **Phase Distribution**: How many fingerprints are in each phase (routing,
  observe, calibrating, drift flagged, excluded).
- **Routing Fingerprints**: The structural feature vectors used to decide
  whether a call is route-eligible.
- **Excluded Fingerprints**: Fingerprints removed from routing.

### 3.6 Sessions

End-to-end attribution for multi-step AI workflows (agent loops, RAG
chains, tool-calling sessions).

**Recent Sessions**: One row per session with total cost, calls, tokens,
attribution confidence, and framework tier.

**Amplification Ratio**: Nodes whose output verbosity drives downstream
costs. High ratio = optimization target.

**Retry Tax**: Cost wasted on retries from rate limits, timeouts, and
errors. Attributed to the node that caused the retry, not the node that
retried.

**Defensive Spend**: Cost of fallback subgraphs and validation calls
triggered by earlier failures or guard rails.

### 3.7 Pricing

Provider pricing data — both vendor-published rates and your custom
overrides.

**Pricing Overrides**: Custom prices you've negotiated with providers,
scoped globally, per-team, or per-app.

**Global Pricing**: Vendor-published list prices Modus uses for cost
calculation when no override applies.

**Override History**: Audit trail of every change to a pricing
override — who, when, before/after values.

### 3.8 Notifications

**Notification Channels**: Configure Slack, Teams, Email, PagerDuty, and
generic webhook delivery.

**App Subscriptions**: Per-app opt-in for threshold alerts, status changes,
and issues.

**Delivery Health**: Success rate, sent/failed counts, per-channel
health, and a list of recent failures with the error reason. If alerts
aren't reaching your Slack channel, this is where you'll see why.

### 3.9 Topology

Live map of your apps, AI providers, frameworks, and service dependencies.
Useful for compliance reviews ("show me everything that touches AI").

### 3.10 Governance

The chain-of-thought ledger and constitutional AI decisions. Used for
regulatory audit trails and post-incident review.

### 3.11 Teams

Teams own apps and are the unit budgets, policies and registration tokens
attach to. Open **Teams** to create, edit, delete and restore them.

**Edit**: Name, slug, description, department, parent team, budgets (overall,
monthly, quarterly), budget period and cost center. Slugs are unique across all
teams, including deleted ones. A team cannot be its own parent or sit under one
of its own descendants. Budgets must be zero or more, with up to 8 decimals.

**Delete**: Deleting is a soft delete, so usage, cost and audit history stay.
If the team still has apps you must choose what happens to them: move them to
another team (their policies, thresholds and live spend counters move too, and
new usage is attributed to the new team), or deactivate them (their API keys
stop working). Child teams must be moved under another team. The team's
registration token is always revoked, so no new agent can self-register into it.
Through the API this is `DELETE /api/v1/teams/{id}` with `reassign_to`,
`cascade=true` and `reassign_children_to`; without them a team that still has
apps or child teams is refused with a 409.

**Restore**: Tick **Show deleted teams** and press **Restore**. Apps that were
moved or deactivated are not brought back, and the registration token must be
generated again. If the former parent team was deleted too, the restored team
becomes top level.

API: `GET /api/v1/teams/{id}`, `PATCH /api/v1/teams/{id}`,
`DELETE /api/v1/teams/{id}`, `POST /api/v1/teams/{id}/restore` and
`GET /api/v1/teams?include_deleted=true` (platform admins). Money is sent and
returned as decimal strings. Every change is written to the audit log.

---

## 4. Common Tasks

Each common task has a step-by-step guide in `docs/guides/`:

| I want to... | Guide |
|---|---|
| Set my first budget cap | [01-set-your-first-budget.md](guides/01-set-your-first-budget.md) |
| Block an expensive model | [02-block-expensive-models.md](guides/02-block-expensive-models.md) |
| Investigate a cost spike | [03-investigate-cost-spike.md](guides/03-investigate-cost-spike.md) |
| Set up cost center chargeback | [04-set-up-chargeback.md](guides/04-set-up-chargeback.md) |
| Route traffic to cheaper models | [05-route-to-cheaper-models.md](guides/05-route-to-cheaper-models.md) |
| Respond to a budget breach alert | [06-handle-budget-breach-alert.md](guides/06-handle-budget-breach-alert.md) |
| Export a finance report | [07-export-finance-report.md](guides/07-export-finance-report.md) |
| Instrument my first application | [08-instrument-first-app.md](guides/08-instrument-first-app.md) |

---

## 5. Glossary of Metrics

| Term | Meaning |
|---|---|
| **MTD** | Month-to-date — cost from the 1st of the current calendar month through now |
| **EOM** | End-of-month — projected total for the full current month |
| **Burn %** | Percent of monthly budget consumed so far this month |
| **Risk** | Classification: `on-track` (below 80% burn), `at-risk` (80% up to 100%), `over-budget` (100% or more) |
| **Z-score** | Standard deviations from a 14-day rolling baseline; ≥3 = significant |
| **R²** | Forecast confidence; ≥0.7 = high, 0.4-0.7 = medium, <0.4 = low |
| **Amplification factor** | (downstream calls + tokens) / (this node's direct cost) |
| **Retry tax** | Cost of retries attributed to the node that *caused* them |
| **Defensive spend** | Cost of fallback subgraphs triggered by earlier failures |
| **Drift score** | How much a routing decision's quality has deviated from baseline |
| **Enforcement mix** | Counts of allowed / blocked / throttled / redirected calls |
| **Online agent** | App SDK that has sent a heartbeat in the last 5 minutes |
| **Cost center** | Accounting bucket teams are assigned to for chargeback |
| **Variance** | Tracked spend minus billed spend for a reconciliation period |

---

## 6. Troubleshooting

**The dashboard is empty / shows "No data".**
Most tiles need at least one app reporting telemetry. If your app isn't
listed in **Agent Registry**, the SDK isn't connecting. See the
[Instrument Your First App](guides/08-instrument-first-app.md) guide.

**An app shows "offline" but I know it's running.**
The agent's heartbeat is older than 5 minutes. Check the app's logs for
SDK errors. Likely causes: a wrong or revoked `MODUS_TEAM_TOKEN`, or an
unreachable `MODUS_URL`. Run `python -m modus diagnose` in the app's
environment.

**My budget cap isn't blocking calls.**
Check **Policies → Enforcement Detail**. If your policy isn't listed in
the recent decisions, it may not be `active`, may have a higher-priority
policy in front of it, or may have a scope mismatch (set to `team` but
your app is in a different team).

**The Finance forecast says "Built-In (Limited Data)".**
With fewer than 3 days of daily cost data the built-in forecaster falls back to
a naive average; from 3 days it uses a trend-plus-weekday model.

**Compare mode shows 0% deltas.**
Make sure you're comparing periods with data on both sides. Brand-new
deployments won't have prior-month data yet.

**I see different cost numbers in different views.**
Overview and Executive use the period selector (default 7D). Finance always uses MTD, so its numbers will not match unless the period
selector is set to match.

---

## 7. FAQ

**Q: Does Modus send my data anywhere?**
Usage data stays in your database. Modus does not report usage to anyone. The
optional features that contact other services are listed in the README's
"Data flow and privacy" table (price-list sync, Nomus rule pull, assistant,
topology summaries, Conductor, federation, and an insight-explanation request
to `api.anthropic.com` that is off by default and needs both an explicit setting and your own Anthropic key).

**Q: Can I run Modus air-gapped?**
Mostly. Leave the Nomus integration unconfigured, keep live pricing fetch
disabled (`MODUS_PRICING_LIVE_FETCH_ENABLED=false`, the default) and leave the
AI insight text settings at their default of `false`. The dashboard
ships its own copies of all CSS, JS and fonts — no CDN calls.

**Q: How accurate is the cost calculation?**
Modus multiplies recorded tokens by your pricing override (if set) or the
bundled vendor list price, so it can differ from your invoice (discounts,
cached tokens, batch pricing). Add pricing overrides for negotiated rates.

**Q: Can I export data?**
Many data tables have CSV and JSON export buttons in the tile header.

**Q: What if a policy blocks a critical call?**
Set the policy `effect` to `warn` instead of `deny`. The call goes through
but a warning is recorded. Use `deny` only after you've confirmed the
warn rate is sustainable.

**Q: How often does the dashboard refresh?**
Every 30 seconds by default for views that show live data (Overview, DevOps,
Executive, Finance, Governance, Compliance, Routing, Sentinel, Sessions,
Federation, Alerts, Apps, Policies, Teams). Change it under Settings,
Auto-refresh (10 s, 30 s, 1 min, 5 min or Off). The timer pauses while the tab
is hidden, never starts a refresh while the previous one is still running, and
bypasses the browser's 30-second response cache. Forms and settings pages are
never refreshed underneath you.

---

## 8. Where to Get Help

- **In-app**: hover the `?` icon on a tile, or open the **Help** view in the
  sidebar for the guide library.
- **Step-by-step guides**: [`docs/guides/`](guides/)
- **GitHub issues**: <https://github.com/babbguy/Modus/issues>

---

*This guide is maintained alongside the Modus codebase. If you find
something out-of-date, please open an issue or PR against
`docs/USER_GUIDE.md`.*
