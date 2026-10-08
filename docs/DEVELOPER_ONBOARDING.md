# Modus — Developer Onboarding

**Audience:** Application developers integrating AI APIs

---

## What you need from your platform team

Two values, that's all:

```
MODUS_URL=https://modus.your-company.com
MODUS_TEAM_TOKEN=mds_team_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Ask whoever runs your Modus orchestrator. They create a team and issue a team
registration token (`POST /api/v1/teams/{team_id}/registration-token`, see the
[deployment guide](deployment.md#authentication)); the token is shown only once.
There is no per-app sign-up: your app registers itself the first time it starts
(`POST /api/v1/self-register`) and receives a short-lived session token
(`mst_...`) that the SDK holds in memory.

---

## Option A — install the SDK (Python)

```bash
# From a clone of this repository (the PyPI package is not published yet):
pip install ./sdk
```

Set environment variables:
```bash
export MODUS_URL=https://modus.your-company.com
export MODUS_TEAM_TOKEN=mds_team_xxxxxxxx
```

Add **one line** to the top of your entry point — before any other imports:

```python
import modus  # MUST be line 1, before anthropic/openai/etc

from fastapi import FastAPI          # rest of your imports below
import anthropic
```

Done. Your AI calls are now tracked and governed.

---

## What import-first means

Modus works by patching AI SDK classes at import time. If an AI SDK is imported before
`import modus`, calls made through that SDK won't be tracked.

**Wrong:**
```python
import openai            # ← patched too late, calls won't be tracked
import modus
```

**Right:**
```python
import modus     # ← line 1, patches everything below
import openai            # ← now tracked
```

---

## Providers tracked automatically

| Provider | Package it patches | Notes |
|---|---|---|
| Anthropic / Claude | `anthropic` | sync, async and streaming |
| OpenAI | `openai` | sync, async and streaming |
| OpenAI-compatible endpoints (xAI, Together, Groq, Mistral, DeepSeek, Fireworks, OpenRouter and others) | `openai` | provider is inferred from the client's `base_url`; unknown hosts are recorded as `openai` |
| Azure OpenAI | `openai` (`AzureOpenAI`) | recorded under provider `openai` |
| Google Gemini | `google-genai` or `google-generativeai` | |
| AWS Bedrock | `boto3` | `bedrock-runtime` clients created after `import modus` |
| Groq | `groq` | sync, async and streaming |
| Mistral | `mistralai` | |
| Cohere | `cohere` | |

If the SDK is installed and imported after `import modus`, it is tracked.
Frameworks that call these packages underneath (for example LangChain or
LlamaIndex) are covered as a side effect. Anything else can be reported with
`agent.record(...)` (below).

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
    # e.reason          — "Daily budget cap of $500 exceeded"
    # e.suggested_model — "claude-haiku-4-5" (platform team can configure this)
    # e.decision        — "deny" or "throttle"
    if e.suggested_model:
        response = anthropic_client.messages.create(
            model=e.suggested_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
    else:
        return {"error": "AI service temporarily unavailable."}
```

---

## Tracking any other provider

```python
import modus  # line 1
from modus import get_agent

agent = get_agent()
if agent:
    agent.record(
        provider="my-llm",
        model="finetuned-v3",
        input_tokens=800,
        output_tokens=200,
    )
```

---

## Test and CI environments

```bash
MODUS_DISABLED=true pytest
```

No warnings. No network calls. No overhead.

---

## Diagnose any environment

```bash
python -m modus diagnose
```

Checks: env vars, network connectivity, token validity, SDK detection, import order.

---

## Kubernetes / Docker

```yaml
# Kubernetes deployment
env:
  - name: MODUS_URL
    valueFrom:
      secretKeyRef: { name: modus, key: url }
  - name: MODUS_TEAM_TOKEN
    valueFrom:
      secretKeyRef: { name: modus, key: team-token }
```

```yaml
# Docker Compose
environment:
  - MODUS_URL=https://modus.your-company.com
  - MODUS_TEAM_TOKEN=mds_team_xxxxxxxx
```

---

## Verify startup

Look for these lines in your logs:

```
INFO  Modus: registered 'my-service' in team 'data-science'
INFO  Modus active | app=my-service env=production providers=[anthropic, openai] fail_open=True v1.0.0
```

(The message goes through Python's `logging`; make sure the `modus` logger
level is INFO or lower to see it.) If the provider list is empty, an AI SDK was
imported before `import modus` or is not installed. Move `import modus` to
line 1.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Modus not started` | Set MODUS_URL and MODUS_TEAM_TOKEN, restart |
| App not in dashboard | Make an AI call; wait 30s for flush |
| Empty provider list in the `Modus active` line | Move `import modus` to line 1 |
| `registration failed ... Governance disabled` | Check MODUS_URL and the team token; run `python -m modus diagnose` |
| Warning spam in tests | Set `MODUS_DISABLED=true` |

---

## Option B — Gateway proxy (any language)

No SDK needed. Point your existing AI client at the Modus gateway instead of the provider directly.

### OpenAI-compatible apps (Python, Node, Go, Rust, etc.)

```bash
# Instead of https://api.openai.com
export OPENAI_BASE_URL=https://modus.your-company.com/gateway/openai/v1
export OPENAI_API_KEY=sk-your-real-openai-key
```

The gateway is off by default (`MODUS_GATEWAY_ENABLED=true` on the orchestrator).
Besides the base URL, each request must carry your app's Modus key in an
`X-Modus-APIKey` header. Get a stable `mds_` key from your admin; they create it
with `POST /api/v1/apps/register` and the master key (the key is shown once).
Clients do not add that header on their own, so set it as a default header:

```python
from openai import OpenAI
client = OpenAI(  # base URL comes from OPENAI_BASE_URL
    default_headers={"X-Modus-APIKey": "mds_your_app_key"},
)
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}]
)
```

```javascript
// Node.js
import OpenAI from "openai";
const client = new OpenAI({
  defaultHeaders: { "X-Modus-APIKey": "mds_your_app_key" },
}); // base URL comes from OPENAI_BASE_URL
```

Other languages: set the client's base URL to
`https://modus.your-company.com/gateway/openai/v1` and add the same header.

### Anthropic-compatible apps

```bash
export ANTHROPIC_BASE_URL=https://modus.your-company.com/gateway/anthropic
export ANTHROPIC_API_KEY=sk-ant-your-real-key
# plus the header X-Modus-APIKey: mds_your_app_key on every request
```

Governed endpoints are `POST /gateway/openai/v1/chat/completions`,
`POST /gateway/openai/v1/embeddings` and `POST /gateway/anthropic/v1/messages`.
Other paths under `/gateway/openai/` and `/gateway/anthropic/` are forwarded
without policy evaluation (the Modus key is still required).

### Authentication

The gateway needs two keys:

| Header | Purpose |
|---|---|
| `X-Modus-APIKey` | Identifies your app to Modus |
| `Authorization` / provider-specific header | Passed through to the AI provider |

The gateway reads your Modus key from `X-Modus-APIKey` header, then forwards the original auth header to the upstream provider untouched.

### Attribution headers (optional)

Add these headers to correlate costs with features:

```bash
curl https://modus.your-company.com/gateway/openai/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "X-Modus-APIKey: mds_your_app_key" \
  -H "X-Modus-Session-ID: checkout-flow-abc123" \
  -H "X-Modus-Span-Name: generate-summary" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}'
```

| Header | Purpose |
|---|---|
| `X-Modus-Session-ID` | Groups calls into a cost attribution session |
| `X-Modus-Span-Name` | Names this operation (e.g. "classify", "summarize") |
| `X-Modus-Parent-ID` | Links to a parent span for call graph tracing |
| `X-Modus-Call-ID` | Your own unique ID for this call |

### Migrating from Helicone

The gateway accepts `Helicone-Auth` as an alias for `X-Modus-APIKey`. Swap your base URL and you're done:

Send `Helicone-Auth: Bearer mds_your_app_key` and change the base URL to
`https://modus.your-company.com/gateway/openai/v1`.

### What the gateway does

1. Authenticates your app via `X-Modus-APIKey`
2. Evaluates governance policies (budget caps, model restrictions)
3. Forwards the request to the real provider (with your original auth)
4. Streams the response back to you in real time
5. Records usage (tokens, cost, model) in the background

Streaming responses are forwarded as they arrive. The gateway adds one policy
evaluation and one network hop per request; see [benchmarking.md](benchmarking.md)
to measure the overhead on your own hardware.

### Gateway vs SDK — which to use?

| | Python SDK | Gateway Proxy |
|---|---|---|
| **Languages** | Python only | Any language |
| **Setup** | `import modus` | Change `BASE_URL` env var |
| **Features** | Policy checks, usage tracking, plus SDK-side routing and an opt-in response cache | Policy checks and usage tracking |
| **Best for** | Python services | Non-Python, multi-language orgs |

---

## Cost attribution (optional)

Track which features drive AI cost using sessions and spans.

### Python SDK

```python
import modus
from modus import get_agent

agent = get_agent()

with agent.session("checkout-flow"):
    with agent.span("classify-intent"):
        # AI call here — automatically attributed
        response = client.chat.completions.create(...)

    with agent.span("generate-response"):
        response = client.chat.completions.create(...)
```

### Non-Python (via gateway headers)

See the attribution headers section under Option B above.

### Viewing attribution data

Attribution data appears in the dashboard's **Sessions** view (recent sessions
with cost and framework tier, amplification ratio, retry tax and defensive
spend).

---

Questions or bugs: open an issue at <https://github.com/babbguy/Modus/issues>.
