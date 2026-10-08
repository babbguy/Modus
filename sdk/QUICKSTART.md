# Modus — Developer Quickstart

Get your application tracked and governed in under 5 minutes.

---

## ⚠ Import order matters — read this first

`import modus` **must be the first line** of your application entry point,
before any AI SDK imports. This is how it patches the SDKs.

```python
import modus     # ← LINE 1, always

from fastapi import FastAPI
import anthropic          # ← tracked because modus was first
import openai             # ← tracked
```

If you put it anywhere else, some or all calls won't be tracked.

---

## Step 1 — Get two values from your platform team

```
MODUS_URL=https://modus.your-company.com
MODUS_TEAM_TOKEN=mds_team_xxxxxxxx
```

Ask whoever runs your Modus orchestrator; they issue the team token (see
[sdk/README.md](README.md#step-1--your-admin-does-this-once-per-team)).
No per-app account creation or pre-registration: the app registers itself.

---

## Step 2 — Install

```bash
# From the repository root (a PyPI release is not published yet):
pip install ./sdk
```

Or add to `requirements.txt`:
```
modus-agent @ git+https://github.com/babbguy/Modus.git#subdirectory=sdk
```

---

## Step 3 — Set environment variables

**Local development (.env):**
```
MODUS_URL=https://modus.your-company.com
MODUS_TEAM_TOKEN=mds_team_xxxxxxxx
MODUS_ENVIRONMENT=dev
```

**Shell:**
```bash
export MODUS_URL=https://modus.your-company.com
export MODUS_TEAM_TOKEN=mds_team_xxxxxxxx
```

**Kubernetes:**
```bash
kubectl create secret generic modus \
  --from-literal=url=https://modus.your-company.com \
  --from-literal=team-token=mds_team_xxxxxxxx
```

---

## Step 4 — Add one import (line 1)

```python
import modus  # ← line 1, before everything else
```

**FastAPI:**
```python
import modus  # ← line 1
from fastapi import FastAPI

app = FastAPI()

@app.post("/api/chat")
async def chat(request: ChatRequest):
    response = anthropic_client.messages.create(...)  # tracked + governed
    return response
```

**Flask:**
```python
import modus  # ← line 1
from flask import Flask

app = Flask(__name__)
```

**Script or worker:**
```python
import modus  # ← line 1
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(...)  # tracked
```

---

## What providers are tracked

| Provider | SDK | Auto-tracked? |
|---|---|---|
| Anthropic / Claude | `anthropic` | ✓ sync + async + streaming |
| OpenAI / ChatGPT | `openai` | ✓ sync + async + streaming |
| xAI / Grok | `openai` (xai base_url) | ✓ automatic |
| Google Gemini | `google-genai` or `google-generativeai` | ✓ |
| AWS Bedrock | `boto3` | ✓ all Bedrock models |
| Groq | `groq` | ✓ sync + async + streaming |
| Mistral | `mistralai` | ✓ sync + async |
| Cohere | `cohere` | ✓ sync + async |
| Azure OpenAI | `openai` (AzureOpenAI client) | ✓ automatic (recorded as provider `openai`) |
| LangChain / LlamaIndex | via the SDKs above | ✓ when they call a package listed above |
| Any other provider | — | Use `agent.record()` |

---

## Handling blocked calls

```python
from modus import PolicyViolationError

try:
    response = anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
except PolicyViolationError as e:
    # e.reason          — "Daily budget of $500 exceeded"
    # e.suggested_model — alternative model ("claude-haiku-4-5")
    # e.decision        — "deny" or "throttle"
    if e.suggested_model:
        response = anthropic_client.messages.create(model=e.suggested_model, ...)
    else:
        return {"error": "AI service temporarily unavailable."}
```

---

## Track any other provider

```python
from modus import get_agent

agent = get_agent()
if agent:
    agent.record(
        provider="my-internal-llm",
        model="finetuned-v3",
        input_tokens=800,
        output_tokens=200,
    )
```

---

## Test / CI environments

```bash
MODUS_DISABLED=true pytest
```

Suppresses all Modus activity — no warnings, no network calls.

---

## Diagnose any environment

```bash
python -m modus diagnose
```

Checks env vars, network, token, SDK detection, import order. Run this first if anything seems wrong.

---

## Verify it's working

Startup logs should show:
```
INFO  Modus: registered 'my-service' in team 'data-science'
INFO  Modus active | app=my-service env=production providers=[anthropic, openai] fail_open=True v1.0.0
```

(This goes through Python's `logging`; set the `modus` logger to INFO to see it.)
If the provider list is empty, `import modus` isn't line 1 of your entry point
or no supported AI package is installed.

Open the dashboard — your app appears within 30 seconds of its first AI call.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Modus not started` | Check MODUS_URL and MODUS_TEAM_TOKEN are set |
| Empty provider list in the `Modus active` line | Move `import modus` to line 1 |
| App not in dashboard | Make an AI call; wait 30s for the flush cycle |
| `registration failed ... Governance disabled` | Check MODUS_URL and the team token; run `python -m modus diagnose` |
| Warning noise in tests | Set `MODUS_DISABLED=true` |

Questions or bugs: <https://github.com/babbguy/Modus/issues>
