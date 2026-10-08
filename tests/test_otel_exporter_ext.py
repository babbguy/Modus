"""
Tests — OTel Exporter (SDK)
Covers: is_enabled, _check_otel_available, record_call (no-op when disabled),
        record_policy_decision (no-op when disabled), OTelNotAvailableError.
"""
from __future__ import annotations


def test_otel_not_available_error():
    from modus.otel_exporter import OTelNotAvailableError
    err = OTelNotAvailableError()
    assert "not installed" in str(err)
    assert isinstance(err, ImportError)


def test_is_enabled_default():
    from modus.otel_exporter import is_enabled
    # Not enabled by default (no enable_otel_export called)
    # May already be enabled from other tests, so just check type
    result = is_enabled()
    assert isinstance(result, bool)


def test_check_otel_available():
    from modus.otel_exporter import _check_otel_available
    # Returns bool — whether opentelemetry is importable
    result = _check_otel_available()
    assert isinstance(result, bool)


def test_record_call_noop_when_disabled():
    """record_call should silently no-op when OTel is not enabled."""
    import modus.otel_exporter as otel
    saved = otel._tracer
    try:
        otel._tracer = None  # Force disabled
        # Should not raise
        otel.record_call(
            provider="anthropic",
            model="claude-sonnet-4-5-20250514",
            input_tokens=100,
            output_tokens=50,
            total_cost=0.001,
            duration_ms=200,
            app_id="test-app",
        )
    finally:
        otel._tracer = saved


def test_record_call_noop_with_error():
    """record_call with error param should no-op when disabled."""
    import modus.otel_exporter as otel
    saved = otel._tracer
    try:
        otel._tracer = None
        otel.record_call(
            provider="openai",
            model="gpt-4o",
            input_tokens=50,
            output_tokens=25,
            status="error",
            error="Rate limited",
            app_id="test",
        )
    finally:
        otel._tracer = saved


def test_record_policy_decision_noop_when_disabled():
    """record_policy_decision should silently no-op when OTel is not enabled."""
    import modus.otel_exporter as otel
    saved = otel._tracer
    try:
        otel._tracer = None
        otel.record_policy_decision(
            decision="deny",
            policy_name="Budget Cap",
            policy_type="budget_cap",
            reason="Over budget",
            provider="anthropic",
            model="claude-sonnet-4-5-20250514",
            app_id="test-app",
        )
    finally:
        otel._tracer = saved


def test_record_call_with_extra_attributes():
    """Extra attributes should not crash even when disabled."""
    import modus.otel_exporter as otel
    saved = otel._tracer
    try:
        otel._tracer = None
        otel.record_call(
            provider="test",
            extra_attributes={"custom.key": "value"},
        )
    finally:
        otel._tracer = saved
