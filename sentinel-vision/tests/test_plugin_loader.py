"""Plugin code has exactly one module identity: the package Hermes' loader creates."""

from __future__ import annotations

import importlib
import sys

import pytest
from harness import fake_sources
from harness.plugin_loader import (
    MODULE_NAME,
    PLUGIN_DIR,
    import_plugin_fresh,
    plugin_module,
    plugin_package,
)


def test_module_name_matches_hermes_loader():
    # hermes_cli/plugins_loader.py::_directory_module_name: hermes_plugins.<key with - -> _>
    assert "hermes_plugins." + PLUGIN_DIR.name.replace("-", "_") == MODULE_NAME
    pkg = plugin_package()
    assert pkg.__name__ == MODULE_NAME and pkg.__path__ == [str(PLUGIN_DIR)]
    assert plugin_package() is pkg  # cached


def test_no_top_level_sentinel_module():
    assert "sentinel" not in sys.modules
    assert "sentinel.sources" not in sys.modules
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sentinel.sources")


def test_harness_and_plugin_share_exception_classes():
    sources = plugin_module("sentinel.sources")
    assert sources.__name__ == f"{MODULE_NAME}.sentinel.sources"
    assert fake_sources.SourceError is sources.SourceError
    assert fake_sources.SourceDisconnected is sources.SourceDisconnected
    src = fake_sources.FakeSource(
        fake_sources.Scene(
            name="x", seed=0, fps=1.0, frames=[], labels=[], should_trigger=False, description=""
        )
    )
    with pytest.raises(sources.SourceError):  # the plugin's own class catches the harness error
        src.read()


@pytest.mark.parametrize("bad", ["", ".sentinel", "..x"])
def test_plugin_module_rejects_non_relative_paths(bad):
    with pytest.raises(ValueError):
        plugin_module(bad)


def test_fresh_import_replaces_modules_and_can_be_restored():
    saved = {k: v for k, v in sys.modules.items() if k == MODULE_NAME or k.startswith(MODULE_NAME)}
    original = saved[MODULE_NAME]
    try:
        fresh = import_plugin_fresh()
        assert fresh is not original and sys.modules[MODULE_NAME] is fresh
        assert f"{MODULE_NAME}.sentinel.sources" not in sys.modules  # stale submodules evicted
    finally:
        for key in [k for k in sys.modules if k == MODULE_NAME or k.startswith(MODULE_NAME)]:
            del sys.modules[key]
        sys.modules.update(saved)
    assert plugin_package() is original
