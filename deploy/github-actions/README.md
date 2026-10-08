# Modus GitHub Actions Workflow Templates

Three workflow **templates** in `deploy/github-actions/workflows/`, plus two
reusable workflows and two composite actions that are better aligned with the
current API (listed at the end). The templates are starting points: copy them
and adapt them. They parse as valid workflows and pass `actionlint` (apart from
shellcheck style notes), but they have not been run end to end against a live
cluster or GitHub repository.

## Templates

### 1. `register-app.yml` — one-time app registration
**Where:** copy to a platform-admin repo or an app repo and run it manually.
**What it does:** calls `POST /api/v1/apps/register` with the master key and
stores the returned app key and app id as repository secrets
(`MODUS_API_KEY`, `MODUS_APP_ID`) using a PAT.

**Required secrets:** `MODUS_MASTER_KEY`, `MODUS_URL`, `GH_PAT_SECRETS_WRITE`
(a PAT that can write repository secrets).

Inputs: `team_slug`, `app_id` (a slug), `app_name`, `environment`. It sends
`app_id`, `app_name`, `team_slug`, `environment` with `X-Modus-APIKey`, as the
registration endpoint expects. Application code only needs `import modus` as its
first import.

### 2. `deploy-orchestrator.yml` — Kubernetes deployment
**What it does:** builds `Dockerfile.orchestrator`, pushes it to an AWS ECR
registry (`REGISTRY`, OIDC role `AWS_OIDC_ROLE_ARN`), then runs
`helm upgrade --install` against `deploy/helm/modus` for staging and, behind
an environment approval, production.

**Required secrets:** `KUBE_CONFIG_STAGING`, `KUBE_CONFIG_PROD` (base64
kubeconfigs), `MODUS_MASTER_KEY_STAGING`, `MODUS_MASTER_KEY_PROD`,
`MODUS_JWT_SECRET_STAGING`, `MODUS_JWT_SECRET_PROD`, `DATABASE_URL_STAGING`,
`DATABASE_URL_PROD`, `REGISTRY`, `AWS_OIDC_ROLE_ARN`.

It installs the chart with `secrets.create=true` and the chart's real values
(`orchestrator.config.environment`, `secrets.databaseUrl`, `secrets.masterApiKey`,
`secrets.jwtSecret`). If you manage Kubernetes secrets yourself, drop those
flags and use `database.existingSecret`, `auth.existingSecret` and
`auth.jwt.existingSecret` instead. See
[docs/deployment.md](../../docs/deployment.md#option-5-kubernetes-helm).

### 3. `app-ci-template.yml` — per-app validation
**Where:** copy to `.github/workflows/` in an instrumented application repo.
**What it does:** checks that the secrets are present, pings `/health`, sends a
heartbeat to check the app key, checks for an env template and that the SDK is
a dependency, and on pull requests posts the app's current cost baseline.

**Required secrets:** `MODUS_API_KEY`, `MODUS_APP_ID`, `MODUS_URL`. The cost
baseline comment additionally needs `MODUS_MASTER_KEY` (dashboard data is an
admin read) and is skipped without it.

## Secrets summary

| Secret | Scope | Set by |
|--------|-------|--------|
| `MODUS_MASTER_KEY` | platform repo | manually |
| `MODUS_URL` | org or repo | manually |
| `GH_PAT_SECRETS_WRITE` | platform repo | manually |
| `MODUS_API_KEY` | app repo | `register-app.yml` |
| `MODUS_APP_ID` | app repo | `register-app.yml` |
| `KUBE_CONFIG_*`, `DATABASE_URL_*`, `MODUS_MASTER_KEY_*` | platform repo | manually |

`MODUS_URL` can be set as an organization secret so every repository inherits
it (GitHub: Organization settings, Secrets and variables, Actions).

## Endpoint notes for workflow authors

### Which credential works where

- `POST /api/v1/apps/register` and `POST /api/v1/apps/{id}/rotate-key` need the
  master key (`X-Modus-APIKey: mds_master_...`).
- The per-app key (`mds_...`, header `X-Modus-APIKey`) is accepted by the
  agent-facing endpoints: usage ingest, heartbeat, policy evaluation, the
  gateway and routing outcomes.
- Everything else (notifications, pricing overrides, teams, policies, finance)
  is user-facing and needs the caller's identity: in `jwt` mode an
  `Authorization: Bearer <JWT>` token **or the master key as
  `X-Modus-APIKey`**, in local `stub` mode no header. The per-app key is
  **not** sufficient for these. The master key is a root credential: store it
  only as a protected secret.

### Notification config in CI

With an admin token, a deploy workflow can configure a Slack channel:

```yaml
- name: Configure Modus notifications
  run: |
    curl -s -X PUT ${{ secrets.MODUS_URL }}/api/v1/notifications/config \
      -H "Authorization: Bearer ${{ secrets.MODUS_ADMIN_JWT }}" \
      -H "Content-Type: application/json" \
      -d '{
        "slack": {
          "enabled": true,
          "webhook_url": "${{ secrets.SLACK_WEBHOOK_URL }}",
          "channel": "#cost-alerts",
          "min_severity": "warning"
        }
      }'
```

### Pricing override registration

```yaml
- name: Register pricing override
  run: |
    curl -s -X POST ${{ secrets.MODUS_URL }}/api/v1/pricing/overrides \
      -H "Authorization: Bearer ${{ secrets.MODUS_ADMIN_JWT }}" \
      -H "Content-Type: application/json" \
      -d '{
        "provider": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
        "input_cost_per_1k": "0.0025",
        "output_cost_per_1k": "0.0100",
        "override_reason": "Negotiated rate"
      }'
```

(Use the exact model name your applications send.)

### Container registry

The Helm chart defaults to `ghcr.io/babbguy/modus-orchestrator` with tag
`1.0.0`. No image is published there until a `v*` tag has been released, so
build and push your own image and override it:

```bash
helm upgrade --install modus ./deploy/helm/modus \
  --set orchestrator.image.repository=<your-registry>/modus-orchestrator \
  --set orchestrator.image.tag=<tag>
```

## Reusable workflows and actions that match the API

- `.github/workflows/modus-register.yml` (reusable, `workflow_call`):
  registers an app and writes `MODUS_API_KEY`, `MODUS_ORCHESTRATOR_URL` and
  `MODUS_APP_ID` secrets to the calling repository. See
  [`examples/example-app-registration.yml`](../../examples/example-app-registration.yml).
- `.github/workflows/modus-policy-sync.yml` (reusable): validates, plans and
  applies a policy file (see [guide 01](../../docs/guides/01-set-your-first-budget.md)).
  Authenticates with the master key; installs the SDK from this repository.
- `github-action/action.yml`: policy check (validate, plan or apply) with a PR
  comment; it installs the SDK from its own checkout (`github.action_path`/../sdk),
  because `modus-agent` is not published on PyPI. Pass the master key as
  `modus-token` (an admin JWT also works in `jwt` mode only through the CLI's
  `--token`, not this input).
- `github-action/cost-check/action.yml`: estimates the monthly cost impact of a
  pull request.
