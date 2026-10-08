# Instrument Your First Application

> Get an app reporting AI usage to Modus. **Audience:** developer
> shipping the integration. **Time:** 10 minutes.

## What you'll accomplish

- Install the Modus agent SDK
- Configure two environment variables
- Watch your first call appear in the dashboard

## Before you start

- Python 3.9+ application that calls one of these providers through its
  official package: OpenAI, Anthropic, AWS Bedrock (`boto3`), Google Gemini
  (`google-genai` or `google-generativeai`), Azure OpenAI, Groq, Mistral,
  Cohere, xAI/Grok
- A Modus orchestrator URL and a team registration token (from your
  administrator; see [deployment.md](../deployment.md#authentication))
- Permission to add a dependency and set environment variables in your app

## Steps

### 1. Install the agent SDK

```bash
# From a clone of this repository (the PyPI package is not published yet):
pip install ./sdk
```

The SDK has zero third-party dependencies — it uses Python stdlib only.

### 2. Set environment variables

```bash
export MODUS_URL="https://modus.your-domain.example"
export MODUS_TEAM_TOKEN="mds_team_..."
```

These are the only two required variables. Optional:

- `MODUS_APP_ID` — explicit app identifier (otherwise detected from
  Kubernetes/ECS/Lambda/Compose metadata, the git repository name or the
  directory name)
- `MODUS_APP_NAME` — display name
- `MODUS_ENVIRONMENT` — environment label (`production` by default; `staging`
  or `dev`)

The team is determined by the token.

### 3. Add one import to your app

```python
import modus  # auto-instruments all supported providers on import
```

That's it. No decorators, no wrappers, no code changes to your existing
LLM calls. The SDK monkey-patches the supported provider clients on
import and emits telemetry to your orchestrator.

### 4. Restart your application

The SDK registers the app, sends heartbeats in the background and ships
usage records in batches (every 30 seconds by default).

### 5. Open the dashboard

Navigate to **Overview**. Within a minute or two:

- Your app should appear in **Agent Status** with a green dot
- Your first calls should appear in **Top Apps by Cost**
- The cost chart should start to fill in

## Verify it worked

1. Make a test call from your app:
   ```python
   from openai import OpenAI
   client = OpenAI()
   client.chat.completions.create(
       model="gpt-4o-mini",
       messages=[{"role": "user", "content": "hello"}],
   )
   ```
2. Within 60 seconds, refresh the Overview view
3. Your test call's cost should appear in **Top Models** under `gpt-4o-mini`

## Troubleshooting

**The app shows "offline" or never appears.**
- Check that the orchestrator URL is reachable from the app:
  `curl ${MODUS_URL}/health`
- Check the app's logs for `modus` errors
- Verify the team token is correct (the orchestrator returns `401` if not)
- Run `python -m modus diagnose`

**The app shows "online" but no calls appear.**
- The SDK only emits records for supported providers. Check the supported
  list above.
- If you're using a custom HTTP client (not the official SDK), Modus
  can't auto-instrument. Report usage manually with
  `modus.get_agent().record(provider=..., model=..., input_tokens=...,
  output_tokens=...)` (see [`sdk/QUICKSTART.md`](../../sdk/QUICKSTART.md)).

**Costs look wrong.**
- Modus calculates cost from token counts. If your provider returns
  approximate or batched token counts, the cost will be approximate too.
- Check **Pricing → Global Pricing** to confirm the model's price. Prices
  are bundled with the release; add a pricing override for negotiated rates.

## Next steps

Once your app is reporting:

- [Set Your First Budget](01-set-your-first-budget.md)
- [Block Expensive Models](02-block-expensive-models.md)
- [Route to Cheaper Models](05-route-to-cheaper-models.md)

## Related

- [USER_GUIDE.md → First 5 Minutes](../USER_GUIDE.md#1-welcome--first-5-minutes)
- SDK reference: [`sdk/README.md`](../../sdk/README.md)
- SDK quickstart: [`sdk/QUICKSTART.md`](../../sdk/QUICKSTART.md)
