# Handle a Budget Breach Alert

> Slack just pinged you that the production team is over budget. Now what?
> **Audience:** on-call engineer or FinOps lead. **Time:** 5 minutes.

## What you'll accomplish

- Confirm the breach is real
- Identify the cause
- Stop the bleeding
- Document the incident

## Steps

### 1. Open the Finance view

The **Budget Breach Alerts** tile lists every team currently breached or
predicted to breach. Find the team in question. Click into the team row
in **Budget Burn Rate by Team** for the spend, projected EOM, and burn %.

### 2. Identify the cause

Open **Overview** and use the period selector to look at the last 7 days.
- If the cost chart has a clear spike: follow [Investigate a Cost Spike](03-investigate-cost-spike.md)
- If cost is rising linearly: usage growth, not a spike
- If projected EOM is wildly higher than month-to-date: the forecast is
  warning you about an exponential ramp

### 3. Stop the bleeding (immediate)

Pick one or more:

**Option A — Hard cap (recommended)**
- Create a deny-effect budget cap policy on this team for the rest of
  the month: see [Set Your First Budget](01-set-your-first-budget.md)
  (the cap is applied per app)
- Set the limit to (current_spend + safety_margin)
- This is reversible — delete the policy when budget resets

**Option B — Throttle**
- Create a rate-limit policy with effect `throttle`
- Slows the team without stopping them entirely

**Option C — Expensive model block**
- If a specific model is the cost driver, block just that model:
  see [Block Expensive Models](02-block-expensive-models.md)

### 4. Notify stakeholders

Alerts are delivered through the channels configured in **Notifications**
(Slack, Teams, email, PagerDuty, webhook; each has a minimum severity).
Manually escalate to the team lead and finance owner if the breach is large.

### 5. Document the incident

Add a note to your incident tracker:
- Time of breach
- Root cause (what changed)
- Mitigation taken
- Permanent fix planned

## Verify it worked

1. Watch the **Enforcement Detail** tile in Policies — your new policy
   should appear in decisions within minutes if it's actively blocking.
2. The team's burn % stops climbing.
3. Refresh the breach predictions — the team should drop off the list
   once mitigation is in place.

## Post-incident

Once the budget resets at the start of the next month, decide:
- Was the breach a one-time spike? Remove the temporary cap.
- Was it a permanent usage increase? Increase the budget officially.
- Was it an attack? Rotate the app's key (`POST /api/v1/apps/{id}/rotate-key`
  with the master key), deactivate the app if needed, and review the audit log
  (`GET /api/v1/audit-log`).

## Related

- [USER_GUIDE.md → Finance](../USER_GUIDE.md#33-finance)
- [Investigate a Cost Spike](03-investigate-cost-spike.md)
- [Set Your First Budget](01-set-your-first-budget.md)
