"""
Modus — OpenTelemetry Metrics & Traces Exporter
=====================================================
Optional module that exports Modus's usage data as OTel spans and metrics,
allowing teams to pipe AI cost/usage data into their existing observability
stack (Datadog, Grafana, Jaeger, New Relic, etc.).

Install:
    pip install modus-agent[otel]

Usage:
    import modus
    from modus.otel_exporter import enable_otel_export

    # Export to your OTel collector (default: localhost:4317 gRPC)
    enable_otel_export()

    # Or specify a custom endpoint
    enable_otel_export(endpoint="https://otel.your-company.com:4317")

    # HTTP/protobuf endpoint
    enable_otel_export(
        endpoint="https://otel.your-company.com:4318/v1/traces",
        protocol="http/protobuf",
    )

This module is entirely optional. Modus works perfectly without it.
All data stays within the customer's infrastructure — OTel export sends
data to the customer's own collector, never to any third party.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any, Optional

logger = logging.getLogger("modus.otel")

# Lazy-loaded OTel SDK objects — None until enable_otel_export() is called
_tracer = None
_meter = None
_provider = None
_meter_provider = None

# Metric instruments (created once on enable)
_cost_counter = None
_token_counter = None
_call_counter = None
_duration_histogram = None
_error_counter = None


class OTelNotAvailableError(ImportError):
    """Raised when opentelemetry packages are not installed."""

    def __init__(self):
        super().__init__(
            "OpenTelemetry SDK not installed. "
            "Install with: pip install modus-agent[otel]"
        )


def _check_otel_available():
    """Verify that opentelemetry packages are importable."""
    try:
        import opentelemetry  # noqa: F401
        return True
    except ImportError:
        return False


def enable_otel_export(
    *,
    endpoint: Optional[str] = None,
    protocol: str = "grpc",
    service_name: str = "modus-sdk",
    resource_attributes: Optional[dict[str, str]] = None,
    export_interval_millis: int = 30000,
    headers: Optional[dict[str, str]] = None,
) -> None:
    """
    Enable OpenTelemetry export of Modus usage data.

    Args:
        endpoint: OTel collector endpoint. Defaults to localhost:4317 (gRPC)
                  or localhost:4318 (HTTP).
        protocol: "grpc" or "http/protobuf". Default: "grpc".
        service_name: Service name in OTel resource. Default: "modus-sdk".
        resource_attributes: Extra resource attributes (e.g. {"deployment.environment": "prod"}).
        export_interval_millis: Metric export interval in ms. Default: 30000 (30s).
        headers: Extra headers for the exporter (e.g. API keys for hosted collectors).
    """
    global _tracer, _meter, _provider, _meter_provider
    global _cost_counter, _token_counter, _call_counter, _duration_histogram, _error_counter

    if not _check_otel_available():
        raise OTelNotAvailableError()

    from opentelemetry import trace, metrics
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    # Build resource
    attrs = {"service.name": service_name}
    if resource_attributes:
        attrs.update(resource_attributes)
    resource = Resource.create(attrs)

    # Set up trace exporter
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        trace_exporter = OTLPSpanExporter(
            endpoint=endpoint or "http://localhost:4317",
            headers=headers or {},
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=endpoint or "http://localhost:4317",
            headers=headers or {},
        )
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        trace_exporter = OTLPSpanExporter(
            endpoint=endpoint or "http://localhost:4318/v1/traces",
            headers=headers or {},
        )
        metric_exporter = OTLPMetricExporter(
            endpoint=endpoint or "http://localhost:4318/v1/metrics",
            headers=headers or {},
        )

    # Trace provider
    _provider = TracerProvider(resource=resource)
    _provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer("modus", "1.1.0")

    # Metrics provider
    reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=export_interval_millis,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)
    _meter = metrics.get_meter("modus", "1.1.0")

    # Create metric instruments
    _cost_counter = _meter.create_counter(
        "modus.ai.cost",
        unit="USD",
        description="Total AI provider cost tracked by Modus",
    )
    _token_counter = _meter.create_counter(
        "modus.ai.tokens",
        unit="tokens",
        description="Total tokens consumed across AI providers",
    )
    _call_counter = _meter.create_counter(
        "modus.ai.calls",
        unit="calls",
        description="Total AI provider calls tracked by Modus",
    )
    _duration_histogram = _meter.create_histogram(
        "modus.ai.duration",
        unit="ms",
        description="AI provider call duration in milliseconds",
    )
    _error_counter = _meter.create_counter(
        "modus.ai.errors",
        unit="errors",
        description="AI provider call errors tracked by Modus",
    )

    # Clean shutdown
    atexit.register(_shutdown)

    logger.info(
        "Modus OTel export enabled: %s via %s",
        endpoint or "localhost default",
        protocol,
    )


def _shutdown():
    """Flush and shut down OTel providers."""
    if _provider:
        _provider.shutdown()
    if _meter_provider:
        _meter_provider.shutdown()


def is_enabled() -> bool:
    """Return True if OTel export has been enabled."""
    return _tracer is not None


def record_call(
    *,
    provider: str,
    model: str = "",
    operation: str = "completion",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_cost: float = 0.0,
    duration_ms: int = 0,
    status: str = "ok",
    error: Optional[str] = None,
    app_id: str = "",
    team_id: str = "",
    environment: str = "",
    extra_attributes: Optional[dict[str, Any]] = None,
) -> None:
    """
    Record an AI provider call as an OTel span + metric data points.

    Called automatically by the Modus agent after each intercepted call.
    Can also be called manually for custom tracking.
    """
    if not is_enabled():
        return

    attrs = {
        "ai.provider": provider,
        "ai.model": model,
        "ai.operation": operation,
        "modus.app_id": app_id,
        "modus.team_id": team_id,
        "modus.environment": environment,
    }
    if extra_attributes:
        attrs.update(extra_attributes)

    # Record span
    with _tracer.start_as_current_span(
        f"ai.{provider}.{operation}",
        attributes=attrs,
    ) as span:
        span.set_attribute("ai.input_tokens", input_tokens)
        span.set_attribute("ai.output_tokens", output_tokens)
        span.set_attribute("ai.total_cost_usd", total_cost)
        span.set_attribute("ai.duration_ms", duration_ms)
        if error:
            span.set_attribute("ai.error", error)
            span.set_status(trace_status_error(error))

    # Record metrics
    metric_attrs = {
        "provider": provider,
        "model": model,
        "environment": environment,
    }

    if _call_counter:
        _call_counter.add(1, metric_attrs)
    if _cost_counter and total_cost > 0:
        _cost_counter.add(total_cost, metric_attrs)
    if _token_counter:
        if input_tokens > 0:
            _token_counter.add(input_tokens, {**metric_attrs, "token_type": "input"})
        if output_tokens > 0:
            _token_counter.add(output_tokens, {**metric_attrs, "token_type": "output"})
    if _duration_histogram and duration_ms > 0:
        _duration_histogram.record(duration_ms, metric_attrs)
    if _error_counter and error:
        _error_counter.add(1, metric_attrs)


def record_policy_decision(
    *,
    decision: str,
    policy_name: str = "",
    policy_type: str = "",
    reason: str = "",
    provider: str = "",
    model: str = "",
    app_id: str = "",
) -> None:
    """Record a policy enforcement decision as an OTel span."""
    if not is_enabled():
        return

    with _tracer.start_as_current_span(
        "modus.policy.evaluate",
        attributes={
            "modus.policy.decision": decision,
            "modus.policy.name": policy_name,
            "modus.policy.type": policy_type,
            "modus.policy.reason": reason,
            "ai.provider": provider,
            "ai.model": model,
            "modus.app_id": app_id,
        },
    ):
        pass  # span auto-closes


def trace_status_error(description: str):
    """Create an OTel error status."""
    from opentelemetry.trace import StatusCode, Status
    return Status(StatusCode.ERROR, description)
