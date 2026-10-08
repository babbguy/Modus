# Set Your First Budget

> Stop AI overspend before it happens. **Audience:** FinOps lead or
> engineering manager. **Time:** 5 minutes.

## What you'll accomplish

- Create a monthly budget cap that applies to the apps of one team
- Have Modus deny calls once an app reaches the cap
- Confirm the policy in the Policies view and see its decisions

## Before you start

- You have at least one team registered (the Teams view lists your teams)
- You know roughly what monthly AI spend is acceptable per app in that team
- You have platform admin or team admin access to the API (in `jwt` mode an
  admin bearer token or the master key as `X-Modus-APIKey`; in local `stub`
  mode no header is needed)

## How the cap works

A `budget_cap` policy compares each **app's** spend in the current window
(`hourly`, `daily` or `monthly`, UTC) with `cap_usd`. A policy with scope
`team` applies to every app in that team, and each app is checked against the
cap separately; it is not a combined team total. When an app's spend reaches
the cap, the call is denied (or throttled or warned, depending on `effect`).

## Steps

Policies can be created in the dashboard (**Policies**, then **New Policy**:
pick the type, fill in its fields, choose a team and app for team/app scope),
through the API, or from a policy file. The steps below use the API.

### 1. Find your team's id

```bash
curl -s "$MODUS_URL/api/v1/teams" -H "Authorization: Bearer $ADMIN_JWT"
# local stub mode: no Authorization header needed
```

Note the `id` of the team.

### 2. Create the policy

```bash
curl -s -X POST "$MODUS_URL/api/v1/policies" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production team monthly cap",
    "description": "Hard limit on each production app per month",
    "policy_type": "budget_cap",
    "scope": "team",
    "team_id": "<team id>",
    "effect": "deny",
    "priority": 100,
    "config": {"cap_usd": "5000.00", "period": "monthly"},
    "action": {"message": "Monthly budget cap reached. Contact your engineering manager."}
  }'
```

`scope` is `platform`, `team` or `app` (`team_id` is required for `team` and
`app`, plus `app_id` for `app`); `effect` is `deny`, `throttle` or `warn`;
`priority` runs from 1 to 999 and a lower number is evaluated first.

### Policy files

[`examples/modus-policy.yaml`](../../examples/modus-policy.yaml) shows the
policy-as-code format. With the SDK installed and PyYAML
(`pip install pyyaml`):

```bash
python -m modus policy validate modus-policy.yaml   # offline
export MODUS_URL=http://localhost:8080
export MODUS_MASTER_API_KEY=mds_master_...           # sent as X-Modus-APIKey
python -m modus policy plan  modus-policy.yaml       # what would change
python -m modus policy apply modus-policy.yaml       # create/update/deactivate
```

`apply` matches policies by name: new names are created, changed ones updated,
identical ones left alone, and active policies that are not in the file are
**deactivated**. The whole file is checked first (types, config values, teams,
apps); if anything is wrong the request is rejected with HTTP 422 and the
list of problems, and nothing is changed. Reference a team by its **slug**
(preferred), exact name, or UUID. Re-running `apply` is a no-op.

## Verify it worked

1. Open **Policies**. The policy appears in **Governance Policies** with type
   `budget cap`, scope `team` and status Active.
2. Once an app reaches the cap, **Policies → Enforcement Detail** lists a
   `deny` decision with your policy name and a reason such as
   `Monthly budget cap of $5000.00 exceeded. Current spend: $...`.
3. To force a test, temporarily create a policy with a cap just below an app's
   current month-to-date spend, watch the next call get denied, then delete it.

## Troubleshooting

**My policy is in the list but calls aren't being blocked.**
Check that it is active and that its scope matches: a `team` policy only
applies to apps of that `team_id`. Check `effect`: `warn` never blocks.
Remember that the cap is per app, so an app below the cap is allowed.

**Calls succeed but show up as `warn` in Enforcement Detail.**
The effect is `warn`, not `deny`. Update the policy.

## Related

- [USER_GUIDE.md → Policies](../USER_GUIDE.md#34-policies)
- [Block Expensive Models](02-block-expensive-models.md)
- [Handle a Budget Breach Alert](06-handle-budget-breach-alert.md)
