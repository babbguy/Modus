"""
Example Modus Plugin — Custom Session Hard Cap
======================================================
Drop any .py file into plugins/ — auto-loaded on startup.

This example demonstrates the plugin pattern:
  - on_load() is called once at startup
  - Access the plugin loader registry via get_loaded_plugins()
  - Extend policy evaluation, add custom metrics, or hook into events

Future: event hooks for pre-evaluate, post-ingest, on-anomaly, etc.
"""

import logging

logger = logging.getLogger(__name__)

# Plugin metadata (optional, for registry)
PLUGIN_NAME = "example_custom_policy"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "Example plugin demonstrating custom session budget enforcement"


def on_load():
    """Called once on startup after the plugin is imported."""
    logger.info(
        "Plugin loaded: %s v%s — %s",
        PLUGIN_NAME, PLUGIN_VERSION, PLUGIN_DESCRIPTION,
    )
    # In a real plugin, you might:
    # - Register custom policy types
    # - Add webhook handlers
    # - Set up external integrations (Slack, PagerDuty, etc.)
    # - Override pricing tables for custom billing
