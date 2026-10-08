"""
Modus — Plugin Loader
===============================
Tiny auto-loader for the plugins/ directory.

Drop any .py file into plugins/ — it's imported at startup.
If the module defines on_load(), it's called after import.

No dependencies. No registry. No framework. Just importlib.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent.parent.parent / "plugins"

_loaded_plugins: dict[str, object] = {}


def load_plugins() -> dict[str, object]:
    """
    Scan plugins/ directory and import every .py file (skip __init__, __pycache__).
    Calls module.on_load() if defined.
    Returns dict of loaded module names → module objects.
    """
    if not PLUGIN_DIR.exists():
        PLUGIN_DIR.mkdir(exist_ok=True)
        logger.info("Created plugins/ directory at %s", PLUGIN_DIR)
        return _loaded_plugins

    for file in sorted(PLUGIN_DIR.glob("*.py")):
        if file.name.startswith("_"):
            continue

        name = file.stem
        if name in _loaded_plugins:
            continue  # already loaded

        try:
            spec = importlib.util.spec_from_file_location(
                f"plugins.{name}", str(file)
            )
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if hasattr(module, "on_load"):
                module.on_load()

            _loaded_plugins[name] = module
            logger.info("Loaded plugin: %s", file.name)

        except Exception as e:
            logger.error("Failed to load plugin %s: %s", file.name, e)

    return _loaded_plugins


def get_loaded_plugins() -> dict[str, object]:
    """Return dict of currently loaded plugin names → module objects."""
    return dict(_loaded_plugins)
