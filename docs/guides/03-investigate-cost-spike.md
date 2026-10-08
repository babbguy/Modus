# Investigate a Cost Spike

> You opened the dashboard and the cost chart has a peak. Find out what
> caused it in under 10 minutes. **Audience:** DevOps, on-call.
> **Time:** 8 minutes.

## What you'll accomplish

- Identify the time window of the spike
- Identify the app, model, and provider responsible
- Confirm whether the spike is a regression, an attack, or expected
- Take corrective action if needed

## Before you start

- You're seeing an unexpected peak on the **Overview → Cost Over Time** chart
- You know roughly when it started (the chart's x-axis tells you)

## Steps

### 1. Click the spike on the cost chart

On the Overview view, click the highest data point in the cost chart.
A modal opens showing:

- Total cost for that period
- Top apps that contributed
- Cost split by provider
- Top models for the period

This is the fastest way to localize the spike.

### 2. Identify the dominant app

Look at the **Top Apps** section of the modal. If one app accounts for
>60% of the spike, that's your culprit. Note the app name.

### 3. Drill into the app

Close the modal. Find the app in the **Top Apps by Cost** table on
Overview. Click the row. A new modal opens with:

- Cost / calls / tokens for the selected period
- Cost by model for that app
- Recent alerts for that app

### 4. Check for an anomaly

Open the **DevOps** view. Look at **Anomaly Detection**. If your spike
period shows an anomaly with z-score ≥ 3, click it for its details. (The
z-score scan needs PostgreSQL; on SQLite this tile stays empty.)

### 5. Check recent deploys

Open **DevOps → Cost by Deployment**. If the spike correlates with a
specific git SHA going to production, you have a regression candidate.
Compare cost-per-day before/after that deploy.

### 6. Check provider/model mix

If a single model jumped (e.g. someone shipped code that uses
`gpt-4-turbo` instead of `gpt-4o-mini`), the **Top Models** tile on
Overview will show it.

### 7. Take action

Choose one based on what you found:

| Cause | Action | Guide |
|---|---|---|
| Wrong model in code | Block the model temporarily | [02-block-expensive-models.md](02-block-expensive-models.md) |
| Loop / retry storm | Add a rate limit policy (`rate_limit`, config `max_calls` and `window_seconds`) | [01-set-your-first-budget.md](01-set-your-first-budget.md) shows how to create a policy |
| Legitimate growth | Increase budget; document the change | [01-set-your-first-budget.md](01-set-your-first-budget.md) |
| Attack / abuse | Deactivate the app (Apps view), or rotate its key with `POST /api/v1/apps/{id}/rotate-key` and the master key | — |

## Verify it worked

After taking action, watch the cost chart for the next 30 minutes. If the
spike doesn't return, you've handled it. If it does, escalate.

## Troubleshooting

**The drill modal shows "No model breakdown available."**
The app's records may not carry a model name (for example calls reported
manually with `agent.record()` without `model=`).

## Related

- [USER_GUIDE.md → Overview](../USER_GUIDE.md#31-overview)
- [USER_GUIDE.md → DevOps](../USER_GUIDE.md#32-devops)
- [Handle a Budget Breach Alert](06-handle-budget-breach-alert.md)
