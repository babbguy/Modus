"""
Modus — Prometheus Metrics
===================================
All Prometheus metric definitions in one place.

Metrics are registered at module import time. Importing this module twice
is safe — prometheus_client handles duplicate registration gracefully.

Naming convention follows Prometheus best practices:
    modus_{subsystem}_{name}_{unit}

Exposed at GET /metrics (Prometheus scrape endpoint).

Grafana dashboard queries reference these metric names directly.
See docs/grafana-dashboard.json for a starter dashboard.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# ── Application info ──────────────────────────────────────────────────────────

APP_INFO = Info(
    "modus_app",
    "Modus orchestrator build information",
)

# ── Ingest metrics ─────────────────────────────────────────────────────────────

INGEST_REQUESTS_TOTAL = Counter(
    "modus_ingest_requests_total",
    "Total ingest requests received",
    ["status"],  # "success" | "rejected" | "error"
)

INGEST_RECORDS_TOTAL = Counter(
    "modus_ingest_records_total",
    "Total usage records ingested",
    ["provider", "resource_type"],
)

INGEST_BATCH_SIZE = Histogram(
    "modus_ingest_batch_size",
    "Number of records per ingest batch",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

INGEST_DURATION_SECONDS = Histogram(
    "modus_ingest_duration_seconds",
    "Ingest request processing duration",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

INGEST_DUPLICATES_TOTAL = Counter(
    "modus_ingest_duplicates_total",
    "Total duplicate batch IDs rejected",
)

# ── App registration metrics ──────────────────────────────────────────────────

REGISTRATIONS_TOTAL = Counter(
    "modus_registrations_total",
    "Total app registrations",
    ["environment"],  # "production" | "staging" | "dev"
)

KEY_ROTATIONS_TOTAL = Counter(
    "modus_key_rotations_total",
    "Total API key rotations",
)

# ── Active apps gauge ─────────────────────────────────────────────────────────

ACTIVE_APPS = Gauge(
    "modus_active_apps",
    "Number of active registered apps",
    ["environment"],
)

ONLINE_AGENTS = Gauge(
    "modus_online_agents",
    "Number of agents with a heartbeat in the last N minutes",
)

# ── Cost metrics ───────────────────────────────────────────────────────────────

TOTAL_COST_INGESTED = Counter(
    "modus_cost_ingested_dollars_total",
    "Total AI cost ingested across all apps",
    ["provider"],
)

TOTAL_TOKENS_INGESTED = Counter(
    "modus_tokens_ingested_total",
    "Total tokens ingested across all apps",
    ["provider", "token_type"],  # token_type: "input" | "output"
)

# ── Database metrics ───────────────────────────────────────────────────────────

DB_QUERY_DURATION_SECONDS = Histogram(
    "modus_db_query_duration_seconds",
    "Database query duration",
    ["operation"],  # "ingest_insert" | "aggregate_read" | "app_lookup" | etc.
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

DB_POOL_SIZE = Gauge(
    "modus_db_pool_size",
    "Database connection pool current size",
)

DB_POOL_CHECKED_OUT = Gauge(
    "modus_db_pool_checked_out",
    "Database connections currently checked out from pool",
)

# ── Aggregation task metrics ───────────────────────────────────────────────────

AGGREGATE_RUNS_TOTAL = Counter(
    "modus_aggregate_runs_total",
    "Total aggregation task executions",
    ["status"],  # "success" | "error"
)

AGGREGATE_DURATION_SECONDS = Histogram(
    "modus_aggregate_duration_seconds",
    "Aggregation task duration",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# ── Alert metrics ──────────────────────────────────────────────────────────────

ALERTS_FIRED_TOTAL = Counter(
    "modus_alerts_fired_total",
    "Total alerts fired",
    ["severity"],  # "warning" | "critical"
)

# ── HTTP request metrics (middleware) ──────────────────────────────────────────

HTTP_REQUESTS_TOTAL = Counter(
    "modus_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "modus_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ── Policy evaluation metrics ─────────────────────────────────────────────────

POLICY_EVALUATE_DURATION_SECONDS = Histogram(
    "modus_policy_evaluate_duration_seconds",
    "Policy evaluation request duration",
    ["decision"],  # "allow" | "deny" | "throttle"
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)

# ── Gateway proxy metrics ─────────────────────────────────────────────────────

GATEWAY_REQUESTS_TOTAL = Counter(
    "modus_gateway_requests_total",
    "Gateway proxy requests by provider, endpoint, and decision/status",
    ["provider", "endpoint", "outcome"],  # outcome: allow|deny|throttle|error
)

GATEWAY_UPSTREAM_DURATION_SECONDS = Histogram(
    "modus_gateway_upstream_duration_seconds",
    "Upstream provider response latency (excludes Modus overhead)",
    ["provider"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

GATEWAY_UPSTREAM_STATUS_TOTAL = Counter(
    "modus_gateway_upstream_status_total",
    "Upstream provider HTTP status codes",
    ["provider", "status"],
)

GATEWAY_GUILLOTINE_FIRES_TOTAL = Counter(
    "modus_gateway_guillotine_fires_total",
    "Mid-stream budget cuts (Guillotine) by provider",
    ["provider"],
)

GATEWAY_CIRCUIT_STATE = Gauge(
    "modus_gateway_circuit_open",
    "Circuit breaker state per provider (1=open/tripped, 0=closed)",
    ["provider"],
)


# ── Routing engine metrics ────────────────────────────────────────────────────

ROUTING_DECISIONS_TOTAL = Counter(
    "modus_routing_decisions_total",
    "Total routing decisions made",
    ["decision"],  # "cheap" | "expensive" | "escalated"
)

ROUTING_COST_SAVED_TOTAL = Counter(
    "modus_routing_cost_saved_dollars_total",
    "Total cost saved via routing to cheaper models (USD)",
)

ROUTING_ACTIVE_FINGERPRINTS = Gauge(
    "modus_routing_active_fingerprints",
    "Number of fingerprints in routing phase",
)

ROUTING_OBSERVE_FINGERPRINTS = Gauge(
    "modus_routing_observe_fingerprints",
    "Number of fingerprints in observe phase",
)

ROUTING_ESCALATION_RATE = Gauge(
    "modus_routing_escalation_rate",
    "Rolling escalation rate across all active fingerprints",
)

ROUTING_CALIBRATOR_DURATION_SECONDS = Histogram(
    "modus_routing_calibrator_duration_seconds",
    "Routing calibrator task duration",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

ROUTING_DRIFT_DETECTIONS_TOTAL = Counter(
    "modus_routing_drift_detections_total",
    "Total drift detection events",
)

ROUTING_CALIBRATION_EVENTS = Counter(
    "modus_routing_calibration_events_total",
    "Calibration lifecycle events (promoted, excluded, demoted)",
    ["event"],  # "promoted" | "excluded" | "demoted"
)

ROUTING_DRIFT_EVENTS = Counter(
    "modus_routing_drift_events_total",
    "Drift detection lifecycle events (flagged, re_observe, recovered)",
    ["event"],  # "flagged" | "re_observe" | "recovered"
)
