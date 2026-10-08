# Modus Agent

AI cost governance and policy enforcement for Python applications. The SDK
uses only the Python standard library and declares `requires-python >=3.9`.

## Install

```bash
# From the repository root (a PyPI release is not published yet):
pip install ./sdk

# or straight from GitHub:
pip install "modus-agent @ git+https://github.com/babbguy/Modus.git#subdirectory=sdk"
```

## Deploy — 5 steps, most of which are one-time

### Step 1 — Your admin does this once per team

```bash
# 1. Create a team. This needs admin access: an admin bearer JWT when the
#    orchestrator runs with MODUS_AUTH_MODE=jwt, or no header at all in local
#    stub mode. (The master key is NOT accepted here.)
curl -X POST https://your-orchestrator/api/v1/teams \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"slug": "ml-platform", "name": "ML Platform"}'
# The response contains the team "id".

# 2. Generate the team's registration token (this one uses the master key)
curl -X POST https://your-orchestrator/api/v1/teams/<team id>/registration-token \
  -H "X-Modus-APIKey: mds_master_xxx"
# The response contains "registration_token": "mds_team_..." (shown once).
# Share this token with your team. That's the only secret they need.
```

See the [deployment guide](../docs/deployment.md#authentication) for how to
mint an admin JWT.

### Step 2 — Developers set two environment variables

```bash
export MODUS_URL=https://your-orchestrator.com
export MODUS_TEAM_TOKEN=mds_team_abc123
```

Or in `.env`:
```
MODUS_URL=https://your-orchestrator.com
MODUS_TEAM_TOKEN=mds_team_abc123
```

### Step 3 — Add one line to your application

```python
import modus  # add this at the top of your entry point
```

That's it. The agent:
- Detects your app identity automatically (from git, Kubernetes, ECS, Lambda, or directory name)
- Self-registers with the orchestrator (`POST /api/v1/self-register`) — no manual app creation needed
- Scans your environment (runtime, cloud, framework, AI packages, API routes, service connections)
- Reports the topology, which appears in the dashboard's Topology view
- Instruments `anthropic`, `openai` (and OpenAI-compatible clients), `boto3` Bedrock, `google-genai` / `google-generativeai`, `groq`, `mistralai` and `cohere` calls automatically
- Enforces governance policies before every AI call
- Flushes usage data every 30 seconds (`MODUS_FLUSH_INTERVAL`)

---

## What gets auto-detected

The agent scans your environment on startup and reports to the dashboard:

| Category | What's detected |
|---|---|
| **Runtime** | Python version, platform, hostname |
| **Deployment** | Kubernetes (namespace, deployment), AWS ECS/Lambda, GCP Cloud Run, Azure, Docker Compose, bare metal |
| **Cloud** | AWS, GCP, Azure, or unknown |
| **AI providers** | anthropic, openai, boto3/bedrock, cohere, mistral, groq, replicate, etc. |
| **AI frameworks** | langchain, llama-index, autogen, crewai, dspy, haystack, etc. |
| **Web framework** | FastAPI, Flask, Django, Starlette, aiohttp, etc. |
| **API routes** | All routes extracted from the live framework, AI endpoints flagged |
| **Service connections** | Postgres, Redis, MongoDB, Kafka, S3, vector DBs — detected from env vars |
| **Infrastructure** | SQLAlchemy, Celery, Redis, Kafka clients, etc. |

If the orchestrator is configured with `MODUS_SUMMARY_AGENT` (`anthropic`,
`openai`, `google`, `deepseek` or `ollama`), it runs the topology through that
provider and stores a plain-English summary. Without it (the default) a local
text summary is generated and nothing leaves your infrastructure. Example of an
AI-written summary:

> *"checkout-api is a FastAPI service on Kubernetes (AWS/EKS) that handles e-commerce checkout flows. It uses Anthropic claude-opus-4-6 for fraud detection and claude-haiku-4-5 for address normalisation. Connected to Postgres, Redis, and a Pinecone vector store."*

---

## Optional configuration

| Variable | Default | Description |
|---|---|---|
| `MODUS_URL` | *(required)* | Orchestrator base URL |
| `MODUS_TEAM_TOKEN` | *(required)* | Team registration token |
| `MODUS_APP_ID` | *auto-detected* | Override app slug |
| `MODUS_APP_NAME` | *auto-detected* | Override app display name |
| `MODUS_ENVIRONMENT` | `production` | `production`, `staging`, or `dev` |
| `MODUS_FAIL_OPEN` | `true` | `false` = deny AI calls when orchestrator unreachable |
| `MODUS_FLUSH_INTERVAL` | `30` | Seconds between usage flushes |
| `MODUS_TIMEOUT` | `3.0` | HTTP timeout for orchestrator calls |
| `MODUS_DISABLED` | unset | `true` turns the SDK off completely |
| `MODUS_EVALUATE_CACHE_TTL` | `2.0` | Seconds a policy decision is cached |
| `MODUS_MAX_BUFFER_SIZE` | `10000` | Maximum buffered usage records |
| `MODUS_LOCAL_BUDGET_CAP_USD` | `0` (off) | Client-side spend cap used while the orchestrator is unreachable and fail-open |
| `MODUS_RESPONSE_CACHE` | `false` | Opt-in exact-match response cache (`MODUS_SEMANTIC_CACHE=true` for the semantic variant) |
| `MODUS_ROUTING_ENABLED` | `true` | Allow SDK-side model routing |
| `MODUS_AGGREGATION_ENABLED` | `true` | Send aggregated summaries plus sampled traces (`MODUS_TRACE_SAMPLE_RATE`, default `0.01`) instead of every raw record |

---

## Handling policy violations

```python
from modus import PolicyViolationError

try:
    response = anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
except PolicyViolationError as e:
    print(f"Blocked: {e.reason}")

    if e.suggested_model:
        # Policy may suggest a cheaper alternative
        response = anthropic_client.messages.create(
            model=e.suggested_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
    else:
        raise
```

---

## Kubernetes deployment

```yaml
# deployment.yaml
env:
  - name: MODUS_URL
    value: "https://your-orchestrator.com"
  - name: MODUS_TEAM_TOKEN
    valueFrom:
      secretKeyRef:
        name: modus
        key: team-token
  # Optional: K8s metadata for better topology reporting
  - name: POD_NAMESPACE
    valueFrom:
      fieldRef:
        fieldPath: metadata.namespace
  - name: DEPLOYMENT_NAME
    value: "checkout-api"
```

```bash
kubectl create secret generic modus \
  --from-literal=team-token=mds_team_abc123
```

---

## Docker Compose

```yaml
# docker-compose.yml
services:
  api:
    environment:
      - MODUS_URL=https://your-orchestrator.com
      - MODUS_TEAM_TOKEN=mds_team_abc123
      - MODUS_ENVIRONMENT=staging
      - COMPOSE_SERVICE=api  # used for app_id auto-detection
```

---

## AWS Lambda

```python
# lambda_handler.py
import modus  # cold start: environment discovery + self-registration run here

def handler(event, context):
    # AI calls in here are automatically tracked and governed
    ...
```

```bash
# Environment variables in Lambda console or SAM template
MODUS_URL=https://your-orchestrator.com
MODUS_TEAM_TOKEN=mds_team_abc123
MODUS_ENVIRONMENT=production
```

The agent detects Lambda from the environment (`AWS_LAMBDA_FUNCTION_NAME`) and uses it for the app identity.

---

## What the agent does NOT do

- Does not write files to disk by default (the opt-in semantic cache keeps a local SQLite file)
- Does not store credentials anywhere persistent (the session token is held in memory)
- Does not modify any code outside of the instrumented SDK methods
- Does not send prompt content or response content — usage records carry provider, model, token counts, durations and metadata, not message text
- Does not contact any service other than your orchestrator URL and the LLM providers your own code already calls (the optional OpenTelemetry exporter and rewind hooks go wherever you configure them)

## Policy CLI

```bash
python -m modus policy validate modus-policy.yaml      # offline (needs PyYAML for YAML files)
export MODUS_URL=https://modus.example.com
export MODUS_MASTER_API_KEY=mds_master_...             # root credential: sent as X-Modus-APIKey
python -m modus policy plan   modus-policy.yaml
python -m modus policy apply  modus-policy.yaml [--dry-run]
python -m modus policy export > modus-policy.yaml
```

The master key works in both `stub` and `jwt` auth modes. Without it, pass an
admin JWT with `--token` (sent as `Authorization: Bearer`; `jwt` mode only).
`team:` in a policy file is a team slug (preferred), exact name or UUID. The
accepted policy types are listed in `modus.policy_schema.POLICY_TYPES` and match
the orchestrator exactly.
