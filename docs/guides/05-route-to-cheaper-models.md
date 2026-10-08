# Route to Cheaper Models

> Send eligible calls to cheaper models. **Audience:** DevOps,
> engineering manager. **Time:** 10 minutes.

## What you'll accomplish

- Understand how routing decides which prompt shapes may use a cheaper model
- Watch routing decisions and savings accumulate
- Confirm no quality regressions

## How it works

Routing is performed by the Python SDK and is on by default
(`MODUS_ROUTING_ENABLED` on the orchestrator and in the app environment). Modus
fingerprints each call site by the structure of its prompt and:

1. **Observes** traffic until a fingerprint has enough calls
   (`MODUS_ROUTING_DEFAULT_OBSERVE_THRESHOLD`, default 200)
2. **Calibrates** it using outcomes where the cheaper model's output passed the
   output validator
3. **Routes** to the cheaper model once the agreement rate reaches
   `1 - max_misroute_rate` (the default misroute budget is 1%; a call site can
   set its own with the `@modus.allow_routing(max_misroute_rate=...)` decorator,
   and `@modus.force_model("...")` opts a call site out). Fingerprints that do
   not reach the target are excluded.
4. **Watches** for drift and flags or demotes fingerprints whose behaviour
   changes

A pre-trained generic classifier is not shipped, so nothing is routed until a
fingerprint has been calibrated on your own traffic. You see all of this on the
**Routing** view.

## Before you start

- The app is instrumented with the SDK
- The call site makes enough repeated calls to reach the observation threshold

## Steps

### 1. Open Routing view

Click **Routing** in the sidebar.

### 2. Check the routing summary

The top of the view shows KPI cards: **Cost Saved 30D**, **Calls Routed**,
**Avg Confidence**, **Escalation Rate** and **Current Phase**, plus a **Phase
Distribution** donut.

### 3. Look at fingerprints

The **Routing Fingerprints** table lists every prompt shape Modus has seen
(hash, app, phase, confidence, drift, routed calls, savings). Phases are:

- `observe` — collecting data
- `calibrating` — being evaluated against validator outcomes
- `routing` — actively routing this shape
- `drift_flagged` — behaviour changed
- `excluded` — removed from routing (automatically, or by an operator)

### 4. Pause or reset

Promotion to `routing` is automatic once the agreement target is met. Each row
has a **Pause**/**Resume** button, and the **Excluded Fingerprints** tile has a
reset button that puts an excluded fingerprint back into the lifecycle.

### 5. Watch savings accumulate

The **Savings Over Time** chart shows cumulative dollars saved.
Compare against the same period before routing was enabled.

## Verify it worked

1. After 7 days of routing, compare current week's spend on the routed
   app vs the prior week.
2. Spot-check some routed calls' outputs in your own application to confirm
   quality.
3. Check that the fingerprints' drift values and the escalation rate are stable.

## Troubleshooting

**Nothing is in `routing` phase yet.**
A fingerprint needs enough calls to leave `observe` and enough validated
outcomes to calibrate. Lower `MODUS_ROUTING_DEFAULT_OBSERVE_THRESHOLD` or
`MODUS_ROUTING_MIN_CALIBRATION_SAMPLES` on the orchestrator if your traffic is
low, or check that the call site is not pinned with `force_model`.

**Drift keeps climbing.**
The shape's behaviour is changing. Pause routing for that fingerprint and
investigate (a new prompt template, new content type, model version change?).

**Routing is enabled but I see no savings.**
Check that the source model and target model actually differ in price
(Pricing view) and that fingerprints have reached the `routing` phase.

## Related

- [USER_GUIDE.md → Routing](../USER_GUIDE.md#35-routing)
- [Block Expensive Models](02-block-expensive-models.md)
