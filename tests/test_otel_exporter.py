"""
Tests — OTel Exporter (graceful degradation without opentelemetry installed)
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


def test_is_enabled_returns_false_by_default():
    """OTel export is disabled until explicitly enabled."""
    from modus.otel_exporter import is_enabled
    # Reset global state
    import modus.otel_exporter as mod
    mod._tracer = None
    assert is_enabled() is False


def test_record_call_noop_when_disabled():
    """record_call silently returns when OTel is not enabled."""
    from modus.otel_exporter import record_call
    import modus.otel_exporter as mod
    mod._tracer = None
    # Should not raise
    record_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        total_cost=0.01,
        duration_ms=200,
    )


def test_record_policy_decision_noop_when_disabled():
    """record_policy_decision silently returns when OTel is not enabled."""
    from modus.otel_exporter import record_policy_decision
    import modus.otel_exporter as mod
    mod._tracer = None
    record_policy_decision(
        decision="deny",
        policy_name="budget_cap",
        reason="Budget exceeded",
    )


def test_enable_raises_without_otel_installed():
    """enable_otel_export raises OTelNotAvailableError if opentelemetry not installed."""
    from modus.otel_exporter import enable_otel_export, OTelNotAvailableError
    with patch("modus.otel_exporter._check_otel_available", return_value=False):
        with pytest.raises(OTelNotAvailableError, match="not installed"):
            enable_otel_export()


def test_check_otel_available():
    """_check_otel_available returns bool based on import availability."""
    from modus.otel_exporter import _check_otel_available
    # This returns True if opentelemetry is installed, False otherwise
    result = _check_otel_available()
    assert isinstance(result, bool)
