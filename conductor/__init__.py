"""
Modus Conductor — Org-Level Data Aggregation Service
==========================================================
Collects, reconciles, and serves clean data from all Orchestrators
across the organisation. The Conductor is the single source of truth
for the Dashboard.

Architecture:
    Dashboard  ←  Conductor  ←  Orchestrator(s)  ←  Agent(s)

The Conductor:
  - Maintains a registry of all Orchestrators in the org
  - Receives pushed aggregates from each Orchestrator (push/ack protocol)
  - Validates data completeness (flags missing Orchestrators)
  - Reconciles cross-Orchestrator totals
  - Serves dashboard-compatible API endpoints
  - Uses tiered caching for dashboard performance

Four Laws compliance:
  - Zero external dependencies (stdlib + SQLAlchemy/FastAPI only)
  - Zero data leaving customer infrastructure
  - Works on a $5/mo VPS
  - Non-blocking (all async, background only)

Federation-ready:
  - Same push/ack protocol can be used by a future Federation layer
    sitting above regional Conductors — no teardown required.
"""

__version__ = "1.1.0"
