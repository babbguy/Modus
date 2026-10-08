"""
modus — AI cost governance and policy enforcement.

CRITICAL: This must be the FIRST import in your application entry point,
before any AI SDK imports (anthropic, openai, langchain, boto3, etc.).

    import modus  # <-- line 1 of your entry point

Zero-config setup:
    pip install ./sdk   # from a clone of the repository
    export MODUS_URL=https://modus.your-company.com
    export MODUS_TEAM_TOKEN=mds_team_abc123

Supported providers (auto-instrumented when SDK is installed):
    Anthropic/Claude, OpenAI/ChatGPT, xAI/Grok, Google Gemini,
    AWS Bedrock, Groq, Mistral, Cohere, Azure OpenAI

Manual tracking for any other provider:
    from modus import get_agent
    agent = get_agent()
    if agent:
        agent.record(provider="my-llm", model="v3",
                     input_tokens=500, output_tokens=200)

Handle policy violations:
    from modus import PolicyViolationError
    try:
        response = client.messages.create(...)
    except PolicyViolationError as e:
        # e.reason, e.suggested_model, e.decision
        if e.suggested_model:
            response = client.messages.create(model=e.suggested_model, ...)

Disable in tests / CI:
    MODUS_DISABLED=true pytest

Diagnose any environment:
    python -m modus diagnose
"""

from modus.agent import ModusAgent, PolicyViolationError
from modus._bootstrap import _agent, get_agent  # noqa: F401

__version__ = "1.0.0"

# ── Routing decorators ───────────────────────────────────────────────────────

import functools

from modus.routing_interceptor import _routing_context


def force_model(model_name: str):
    """
    Decorator that forces a specific model for a call site, bypassing routing.
    Use for call sites where output quality is non-negotiable.

    Example:
        @modus.force_model("claude-opus-4-6")
        def generate_legal_summary(text):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _routing_context.force_model = model_name
            _routing_context.call_site_id = func.__qualname__
            try:
                return func(*args, **kwargs)
            finally:
                _routing_context.force_model = None
                _routing_context.call_site_id = None
        return wrapper
    return decorator


def allow_routing(max_misroute_rate: float = 0.01):
    """
    Decorator that opts a call site into routing with an explicit misroute budget.
    The system will not route this call site until calibration meets the budget.

    Example:
        @modus.allow_routing(max_misroute_rate=0.005)
        def classify_support_ticket(text):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _routing_context.max_misroute_rate = max_misroute_rate
            _routing_context.call_site_id = func.__qualname__
            try:
                return func(*args, **kwargs)
            finally:
                _routing_context.max_misroute_rate = None
                _routing_context.call_site_id = None
        return wrapper
    return decorator


__all__ = [
    "ModusAgent",
    "PolicyViolationError",
    "get_agent",
    "force_model",
    "allow_routing",
]
