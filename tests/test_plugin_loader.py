"""
Tests for orchestrator.core.plugin_loader — plugin discovery and loading.
"""
from __future__ import annotations

from unittest.mock import patch


import orchestrator.core.plugin_loader as pl
from orchestrator.core.plugin_loader import (
    _loaded_plugins,
    get_loaded_plugins,
    load_plugins,
)


class TestLoadPlugins:
    def test_creates_dir_if_missing(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        assert not plugin_dir.exists()

        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", {}):
                result = load_plugins()
                assert plugin_dir.exists()
                assert result == {}

    def test_loads_py_files(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        # Write a simple plugin
        (plugin_dir / "hello_plugin.py").write_text("VALUE = 42\n")

        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", {}):
                result = load_plugins()
                assert "hello_plugin" in result
                assert result["hello_plugin"].VALUE == 42

    def test_calls_on_load(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "callback_plugin.py").write_text(
            "LOADED = False\ndef on_load():\n    global LOADED\n    LOADED = True\n"
        )

        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", {}):
                result = load_plugins()
                assert result["callback_plugin"].LOADED is True

    def test_skips_underscore_files(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "__init__.py").write_text("")
        (plugin_dir / "_private.py").write_text("SECRET = 1")

        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", {}):
                result = load_plugins()
                assert "__init__" not in result
                assert "_private" not in result

    def test_handles_broken_plugin(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "broken.py").write_text("raise RuntimeError('boom')")

        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", {}):
                result = load_plugins()
                assert "broken" not in result

    def test_skips_already_loaded(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "repeat.py").write_text("X = 1")

        existing = {"repeat": "already-loaded"}
        with patch.object(pl, "PLUGIN_DIR", plugin_dir):
            with patch.object(pl, "_loaded_plugins", existing):
                result = load_plugins()
                assert result["repeat"] == "already-loaded"


class TestGetLoadedPlugins:
    def test_returns_copy(self):
        result = get_loaded_plugins()
        assert isinstance(result, dict)
        # Should be a copy, not the original
        assert result is not _loaded_plugins
