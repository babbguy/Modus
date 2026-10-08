# Block Expensive Models

> Prevent specific models (e.g. GPT-4 Turbo, Claude Opus) from being
> called by certain teams or apps. **Audience:** DevOps or engineering
> manager. **Time:** 5 minutes.

## What you'll accomplish

- Create a model denylist policy
- Verify that calls to those models are blocked
- Narrow the policy to the environments you care about

## Before you start

- You know the exact model names you want to block (e.g. `gpt-4-turbo`,
  `claude-3-opus-20240229`), as your applications send them
- You know the team or app you want to scope this to
- You have admin access to the API (see [guide 01](01-set-your-first-budget.md))

## Steps

### 1. Create the policy

```bash
curl -s -X POST "$MODUS_URL/api/v1/policies" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Block premium models in dev",
    "policy_type": "model_denylist",
    "scope": "team",
    "team_id": "<team id>",
    "effect": "deny",
    "priority": 100,
    "config": {"models": ["gpt-4-turbo", "claude-3-opus-20240229"]},
    "conditions": {"environments": ["dev"]},
    "action": {
      "message": "Premium models are not allowed in dev. Use staging or get approval.",
      "suggested_model": "gpt-4o-mini"
    }
  }'
```

- `scope` can be `team` (all apps in the team) or `app` (add `app_id`), or
  `platform` for everything.
- `conditions` is optional. It can restrict the policy by `environments`,
  `providers`, `resource_types` or a `model_pattern` glob; all must match.
- `suggested_model` is returned to the SDK, which exposes it as
  `PolicyViolationError.suggested_model`.

The policy-file format (`type: model_denylist`, see
[`examples/modus-policy.yaml`](../../examples/modus-policy.yaml)) describes the
same policy, but see [guide 01](01-set-your-first-budget.md#policy-files-validation-only-for-now)
for why it is validation-only for now.

## Verify it worked

1. Make a test call from an app in the `dev` environment to a denylisted
   model. The SDK raises `PolicyViolationError`; its `reason` is
   `Model 'gpt-4-turbo' is explicitly blocked by policy 'Block premium models in dev'.`
   and `message` is your action message.
2. Open **Policies → Enforcement Detail**. You should see a `deny` row with
   your policy name.
3. The DevOps view's **Enforcement** tile shows the blocked count.

## Narrowing or exempting

Policies are evaluated by scope specificity (app, then team, then platform)
and then priority; the first `deny` or `throttle` wins, and a `warn` never
stops evaluation. There is no "allow override", so to exempt something,
narrow the denylist instead: use `conditions` (for example only the `dev`
environment), scope it to specific apps, or place the exempt app in another
team.

## Troubleshooting

**My denylist isn't blocking the model.**
Model names are compared exactly (case-sensitive) with the entries in
`config.models`. Make sure the string matches what your app sends; **Overview →
Top Models** shows the recorded names. Also check that the policy's scope and
`conditions` match the calling app and its environment.

## Related

- [USER_GUIDE.md → Policies](../USER_GUIDE.md#34-policies)
- [Set Your First Budget](01-set-your-first-budget.md)
- [Route to Cheaper Models](05-route-to-cheaper-models.md)
