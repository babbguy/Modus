"""
modus.policy_schema
==========================
JSON Schema for modus-policy.yaml validation.

Defines the Budget-as-Code schema that allows teams to declare
governance policies as code (YAML files checked into version control).

Usage:
    modus policy validate modus-policy.yaml
    modus policy plan modus-policy.yaml
    modus policy apply modus-policy.yaml
    modus policy export > modus-policy.yaml
"""

from __future__ import annotations

# ── JSON Schema for modus-policy.yaml ─────────────────────────────────────

# MUST stay identical to ``VALID_POLICY_TYPES`` in orchestrator/api/policies.py
# (the SDK is stdlib-only and cannot import the orchestrator;
# tests/test_policy_types_consistency.py asserts the two lists are equal).
POLICY_TYPES = [
    "budget_cap",
    "rate_limit",
    "token_cap",
    "latency_cap",
    "model_allowlist",
    "model_denylist",
    "provider_block",
    "environment_block",
    "degradation_ladder",
    "amplification_gate",
    "retry_circuit_breaker",
]

EFFECTS = ["deny", "throttle", "warn"]

SCOPES = ["platform", "team", "app"]

PERIODS = ["hourly", "daily", "monthly"]

# ── JSON Schema (Draft 2020-12 compatible) ───────────────────────────────────

POLICY_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/babbguy/Modus/schemas/modus-policy.json",
    "title": "Modus Policy File",
    "description": "Declarative governance policies for AI cost control.",
    "type": "object",
    "required": ["version", "policies"],
    "additionalProperties": False,
    "properties": {
        "version": {
            "type": "string",
            "const": "1",
            "description": "Schema version. Must be '1'.",
        },
        "defaults": {
            "type": "object",
            "description": "Default values applied to all policies unless overridden.",
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": SCOPES},
                "effect": {"type": "string", "enum": EFFECTS},
                "team": {"type": "string", "description": "Team slug (preferred), exact team name, or UUID."},
                "environment": {
                    "type": "string",
                    "description": "Target environment (production, staging, etc.).",
                },
            },
        },
        "policies": {
            "type": "array",
            "minItems": 1,
            "description": "List of governance policies.",
            "items": {"$ref": "#/$defs/policy"},
        },
    },
    "$defs": {
        "policy": {
            "type": "object",
            "required": ["name", "type"],
            "additionalProperties": False,
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "Human-readable policy name. Must be unique within scope.",
                },
                "description": {
                    "type": "string",
                    "maxLength": 1024,
                    "description": "Optional description of the policy's purpose.",
                },
                "type": {
                    "type": "string",
                    "enum": POLICY_TYPES,
                    "description": "Policy type.",
                },
                "scope": {
                    "type": "string",
                    "enum": SCOPES,
                    "default": "team",
                    "description": "Scope: platform, team, or app.",
                },
                "effect": {
                    "type": "string",
                    "enum": EFFECTS,
                    "default": "deny",
                    "description": "Action when policy triggers: deny, throttle, or warn.",
                },
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 999,
                    "default": 100,
                    "description": "Evaluation order (lower = first). Default 100.",
                },
                "team": {
                    "type": "string",
                    "description": "Team slug (preferred), exact team name, or UUID. Required for team/app scope.",
                },
                "app": {
                    "type": "string",
                    "description": "App name or UUID. Required for app scope.",
                },
                "suggested_model": {
                    "type": "string",
                    "description": "Fallback model suggested when policy triggers.",
                },
                "conditions": {"$ref": "#/$defs/conditions"},
                "config": {"$ref": "#/$defs/config"},
                "action": {"$ref": "#/$defs/action"},
                "enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether the policy is active.",
                },
            },
            "allOf": [
                # budget_cap requires config.cap_usd + config.period
                {
                    "if": {"properties": {"type": {"const": "budget_cap"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["cap_usd", "period"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # rate_limit requires config.max_calls + config.window_seconds
                {
                    "if": {"properties": {"type": {"const": "rate_limit"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["max_calls", "window_seconds"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # token_cap requires config.max_tokens + config.period
                {
                    "if": {"properties": {"type": {"const": "token_cap"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["max_tokens", "period"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # latency_cap requires config.max_ms
                {
                    "if": {"properties": {"type": {"const": "latency_cap"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["max_ms"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # model_allowlist requires config.models
                {
                    "if": {"properties": {"type": {"const": "model_allowlist"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["models"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # model_denylist requires config.models
                {
                    "if": {"properties": {"type": {"const": "model_denylist"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["models"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # provider_block requires config.providers
                {
                    "if": {"properties": {"type": {"const": "provider_block"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["providers"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # environment_block requires config.environments
                {
                    "if": {"properties": {"type": {"const": "environment_block"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["environments"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # degradation_ladder requires config.budget_usd + config.period + config.tiers
                {
                    "if": {"properties": {"type": {"const": "degradation_ladder"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["budget_usd", "period", "tiers"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # amplification_gate requires config.max_amplification
                {
                    "if": {"properties": {"type": {"const": "amplification_gate"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["max_amplification"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
                # retry_circuit_breaker requires config.max_retries
                {
                    "if": {"properties": {"type": {"const": "retry_circuit_breaker"}}},
                    "then": {
                        "properties": {
                            "config": {
                                "required": ["max_retries"],
                            }
                        },
                        "required": ["name", "type", "config"],
                    },
                },
            ],
        },
        "conditions": {
            "type": "object",
            "description": "Request-level filters. All specified conditions must match (AND logic).",
            "additionalProperties": False,
            "properties": {
                "providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Provider names (e.g., anthropic, openai).",
                },
                "model_pattern": {
                    "type": "string",
                    "description": "Glob pattern for model matching (e.g., 'gpt-4*').",
                },
                "environments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Environment names (e.g., production, staging).",
                },
                "resource_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Resource types (default: llm_call).",
                },
            },
        },
        "config": {
            "type": "object",
            "description": "Policy-type-specific configuration.",
            "properties": {
                # budget_cap
                "cap_usd": {
                    "type": "string",
                    "pattern": r"^\d+(\.\d{1,2})?$",
                    "description": "Budget cap in USD (e.g., '100.00').",
                },
                "period": {
                    "type": "string",
                    "enum": PERIODS,
                    "description": "Time window: hourly, daily, or monthly.",
                },
                # rate_limit
                "max_calls": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum calls allowed in window.",
                },
                "window_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Window size in seconds for rate_limit.",
                },
                # token_cap
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum tokens allowed in period.",
                },
                # latency_cap
                "max_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum average latency in ms.",
                },
                # model_allowlist / model_denylist
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of model names.",
                },
                # provider_block
                "providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of provider names to block.",
                },
                # environment_block
                "environments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "List of environments to block.",
                },
                # degradation_ladder
                "budget_usd": {
                    "type": "string",
                    "pattern": r"^\d+(\.\d{1,2})?$",
                    "description": "Total budget for degradation ladder.",
                },
                "tiers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["pct"],
                        "additionalProperties": False,
                        "properties": {
                            "pct": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                                "description": "Budget percentage threshold.",
                            },
                            "model": {
                                "type": "string",
                                "description": "Model to downshift to.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["downshift", "deny"],
                                "description": "Action at this tier.",
                            },
                        },
                    },
                    "description": "Degradation tiers (sorted by pct ascending).",
                },
                # amplification_gate
                "max_amplification": {
                    "type": "number",
                    "minimum": 1.0,
                    "description": "Maximum amplification factor.",
                },
                # retry_circuit_breaker
                "max_retries": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum retries before circuit breaks.",
                },
                "window_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Window in minutes for retry counting.",
                },
            },
        },
        "action": {
            "type": "object",
            "description": "Action metadata returned to the agent on trigger.",
            "additionalProperties": False,
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Human-readable message shown to the developer.",
                },
                "retry_after_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Seconds to wait before retry (for throttle).",
                },
            },
        },
    },
}


# ── Validation (stdlib only — no jsonschema dependency) ──────────────────────

def validate_policy_file(data: dict) -> list[str]:
    """
    Validate a parsed policy YAML/JSON against the schema.

    Returns a list of error strings. Empty list = valid.
    Uses stdlib only — no jsonschema dependency required.
    """
    errors: list[str] = []

    # Top-level structure
    if not isinstance(data, dict):
        return ["Policy file must be a YAML mapping (object)."]

    if data.get("version") != "1":
        errors.append("'version' must be '1'.")

    policies = data.get("policies")
    if not isinstance(policies, list) or len(policies) == 0:
        errors.append("'policies' must be a non-empty list.")
        return errors

    # Validate defaults
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        errors.append("'defaults' must be a mapping.")
        defaults = {}

    valid_top_keys = {"version", "defaults", "policies"}
    extra_top = set(data.keys()) - valid_top_keys
    if extra_top:
        errors.append(f"Unknown top-level keys: {', '.join(sorted(extra_top))}")

    # Validate each policy
    names_seen: set[str] = set()
    for i, policy in enumerate(policies):
        prefix = f"policies[{i}]"

        if not isinstance(policy, dict):
            errors.append(f"{prefix}: must be a mapping.")
            continue

        # Required fields
        name = policy.get("name")
        if not name or not isinstance(name, str):
            errors.append(f"{prefix}: 'name' is required and must be a non-empty string.")
        elif name in names_seen:
            errors.append(f"{prefix}: duplicate policy name '{name}'.")
        else:
            names_seen.add(name)
            prefix = f"policies[{i}] ({name})"

        ptype = policy.get("type")
        if ptype not in POLICY_TYPES:
            errors.append(
                f"{prefix}: 'type' must be one of: {', '.join(POLICY_TYPES)}. "
                f"Got: {ptype!r}"
            )
            continue

        # Merge defaults
        scope = policy.get("scope", defaults.get("scope", "team"))
        effect = policy.get("effect", defaults.get("effect", "deny"))

        if scope not in SCOPES:
            errors.append(f"{prefix}: 'scope' must be one of: {', '.join(SCOPES)}.")
        if effect not in EFFECTS:
            errors.append(f"{prefix}: 'effect' must be one of: {', '.join(EFFECTS)}.")

        priority = policy.get("priority", 100)
        if not isinstance(priority, int) or priority < 1 or priority > 999:
            errors.append(f"{prefix}: 'priority' must be an integer 1–999.")

        # Scope requires team/app
        if scope in ("team", "app"):
            team = policy.get("team", defaults.get("team"))
            if not team:
                errors.append(f"{prefix}: '{scope}' scope requires 'team'.")
        if scope == "app":
            if not policy.get("app"):
                errors.append(f"{prefix}: 'app' scope requires 'app'.")

        # Config validation by type
        config = policy.get("config", {})
        if not isinstance(config, dict):
            errors.append(f"{prefix}: 'config' must be a mapping.")
            continue

        errors.extend(_validate_config(prefix, ptype, config))

        # Conditions validation
        conditions = policy.get("conditions")
        if conditions is not None:
            if not isinstance(conditions, dict):
                errors.append(f"{prefix}: 'conditions' must be a mapping.")
            else:
                valid_cond_keys = {"providers", "model_pattern", "environments", "resource_types"}
                extra_cond = set(conditions.keys()) - valid_cond_keys
                if extra_cond:
                    errors.append(f"{prefix}.conditions: unknown keys: {', '.join(sorted(extra_cond))}")

        # Action validation
        action = policy.get("action")
        if action is not None:
            if not isinstance(action, dict):
                errors.append(f"{prefix}: 'action' must be a mapping.")

        # Unknown keys
        valid_policy_keys = {
            "name", "description", "type", "scope", "effect", "priority",
            "team", "app", "suggested_model", "conditions", "config",
            "action", "enabled",
        }
        extra = set(policy.keys()) - valid_policy_keys
        if extra:
            errors.append(f"{prefix}: unknown keys: {', '.join(sorted(extra))}")

    return errors


def _validate_config(prefix: str, ptype: str, config: dict) -> list[str]:
    """Validate policy-type-specific config fields."""
    errors: list[str] = []

    if ptype == "budget_cap":
        if "cap_usd" not in config:
            errors.append(f"{prefix}.config: 'cap_usd' is required for budget_cap.")
        elif not isinstance(config["cap_usd"], str) or not _is_usd(config["cap_usd"]):
            errors.append(f"{prefix}.config: 'cap_usd' must be a string like '100.00'.")
        if "period" not in config:
            errors.append(f"{prefix}.config: 'period' is required for budget_cap.")
        elif config["period"] not in PERIODS:
            errors.append(f"{prefix}.config: 'period' must be one of: {', '.join(PERIODS)}.")

    elif ptype == "rate_limit":
        if "max_calls" not in config:
            errors.append(f"{prefix}.config: 'max_calls' is required for rate_limit.")
        elif not isinstance(config["max_calls"], int) or config["max_calls"] < 1:
            errors.append(f"{prefix}.config: 'max_calls' must be a positive integer.")
        if "window_seconds" not in config:
            errors.append(f"{prefix}.config: 'window_seconds' is required for rate_limit.")
        elif not isinstance(config["window_seconds"], int) or config["window_seconds"] < 1:
            errors.append(f"{prefix}.config: 'window_seconds' must be a positive integer.")

    elif ptype == "token_cap":
        if "max_tokens" not in config:
            errors.append(f"{prefix}.config: 'max_tokens' is required for token_cap.")
        elif not isinstance(config["max_tokens"], int) or config["max_tokens"] < 1:
            errors.append(f"{prefix}.config: 'max_tokens' must be a positive integer.")
        if "period" not in config:
            errors.append(f"{prefix}.config: 'period' is required for token_cap.")
        elif config["period"] not in PERIODS:
            errors.append(f"{prefix}.config: 'period' must be one of: {', '.join(PERIODS)}.")

    elif ptype == "latency_cap":
        if "max_ms" not in config:
            errors.append(f"{prefix}.config: 'max_ms' is required for latency_cap.")
        elif not isinstance(config["max_ms"], int) or config["max_ms"] < 1:
            errors.append(f"{prefix}.config: 'max_ms' must be a positive integer.")
        period = config.get("period")
        if period is not None and period not in PERIODS:
            errors.append(f"{prefix}.config: 'period' must be one of: {', '.join(PERIODS)}.")

    elif ptype in ("model_allowlist", "model_denylist"):
        if "models" not in config:
            errors.append(f"{prefix}.config: 'models' is required for {ptype}.")
        elif not isinstance(config["models"], list) or len(config["models"]) == 0:
            errors.append(f"{prefix}.config: 'models' must be a non-empty list.")

    elif ptype == "provider_block":
        if "providers" not in config:
            errors.append(f"{prefix}.config: 'providers' is required for provider_block.")
        elif not isinstance(config["providers"], list) or len(config["providers"]) == 0:
            errors.append(f"{prefix}.config: 'providers' must be a non-empty list.")

    elif ptype == "environment_block":
        if "environments" not in config:
            errors.append(f"{prefix}.config: 'environments' is required for environment_block.")
        elif not isinstance(config["environments"], list) or len(config["environments"]) == 0:
            errors.append(f"{prefix}.config: 'environments' must be a non-empty list.")

    elif ptype == "degradation_ladder":
        if "budget_usd" not in config:
            errors.append(f"{prefix}.config: 'budget_usd' is required for degradation_ladder.")
        elif not isinstance(config["budget_usd"], str) or not _is_usd(config["budget_usd"]):
            errors.append(f"{prefix}.config: 'budget_usd' must be a string like '500.00'.")
        if "period" not in config:
            errors.append(f"{prefix}.config: 'period' is required for degradation_ladder.")
        elif config["period"] not in PERIODS:
            errors.append(f"{prefix}.config: 'period' must be one of: {', '.join(PERIODS)}.")
        tiers = config.get("tiers")
        if not isinstance(tiers, list) or len(tiers) == 0:
            errors.append(f"{prefix}.config: 'tiers' must be a non-empty list.")
        elif isinstance(tiers, list):
            for j, tier in enumerate(tiers):
                if not isinstance(tier, dict):
                    errors.append(f"{prefix}.config.tiers[{j}]: must be a mapping.")
                    continue
                if "pct" not in tier:
                    errors.append(f"{prefix}.config.tiers[{j}]: 'pct' is required.")
                elif not isinstance(tier["pct"], int) or tier["pct"] < 0 or tier["pct"] > 100:
                    errors.append(f"{prefix}.config.tiers[{j}]: 'pct' must be 0–100.")
                if "model" not in tier and tier.get("action") != "deny":
                    errors.append(
                        f"{prefix}.config.tiers[{j}]: must have 'model' or 'action: deny'."
                    )

    elif ptype == "amplification_gate":
        if "max_amplification" not in config:
            errors.append(f"{prefix}.config: 'max_amplification' is required for amplification_gate.")
        elif not isinstance(config["max_amplification"], (int, float)) or config["max_amplification"] < 1:
            errors.append(f"{prefix}.config: 'max_amplification' must be >= 1.0.")

    elif ptype == "retry_circuit_breaker":
        if "max_retries" not in config:
            errors.append(f"{prefix}.config: 'max_retries' is required for retry_circuit_breaker.")
        elif not isinstance(config["max_retries"], int) or config["max_retries"] < 1:
            errors.append(f"{prefix}.config: 'max_retries' must be a positive integer.")
        wm = config.get("window_minutes")
        if wm is not None and (not isinstance(wm, int) or wm < 1):
            errors.append(f"{prefix}.config: 'window_minutes' must be a positive integer.")

    return errors


def _is_usd(s: str) -> bool:
    """Check if string is a valid USD amount (e.g., '100', '100.00', '0.50')."""
    import re
    return bool(re.match(r"^\d+(\.\d{1,2})?$", s))
