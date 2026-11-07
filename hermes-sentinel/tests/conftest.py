"""Shared fixtures: a recording FakeCtx and the plugin loaded into it, no live Hermes needed.

Plugin code is always reached through the package Hermes' loader creates
(``hermes_plugins.hermes_sentinel``, see :mod:`harness.plugin_loader`), never as a top-level
``sentinel`` module, so exception classes are the same objects everywhere.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from harness import hermes_shims
from harness.fake_ctx import FakeCtx
from harness.plugin_loader import MODULE_NAME, PLUGIN_DIR, PLUGIN_NAME, plugin_package

__all__ = ["MODULE_NAME", "PLUGIN_DIR", "PLUGIN_NAME", "LoadedPlugin"]

# Load the package (and the Hermes module shims it may import) at collection time.
plugin_package()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    hermes_shims.uninstall()


@dataclass
class LoadedPlugin:
    module: types.ModuleType
    ctx: FakeCtx


@pytest.fixture
def plugin_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch Hermes home. HERMES_HOME points here so nothing can reach the real ~/.hermes."""
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def secrets() -> dict[str, str]:
    """Secrets ``agent.secret_scope.get_secret`` returns. Override in a test module to seed."""
    return {}


@pytest.fixture
def shim_binding(plugin_data_root: Path, secrets: dict[str, str]):
    """Bind the storage/secret shims to the scratch home and the ``secrets`` dict."""
    with hermes_shims.bind(home=plugin_data_root, secrets=secrets) as binding:
        yield binding


@pytest.fixture
def fake_ctx(shim_binding: hermes_shims.ShimBinding):
    ctx = FakeCtx(plugin_id=PLUGIN_NAME)
    yield ctx
    ctx.close()


@pytest.fixture
def plugin_data_dir(shim_binding: hermes_shims.ShimBinding) -> Path:
    """``<tmp hermes home>/plugin-data/hermes-sentinel`` (created), via the public module API."""
    from plugins.plugin_storage import plugin_data_dir as _plugin_data_dir

    return _plugin_data_dir(PLUGIN_NAME)


@pytest.fixture
def loaded_plugin(fake_ctx: FakeCtx):
    """The plugin package (loaded once per session) registered on a fresh FakeCtx."""
    module = plugin_package()
    module.register(fake_ctx)
    yield LoadedPlugin(module=module, ctx=fake_ctx)


@pytest.fixture
def no_real_device_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch real cameras: the setup check and ``sentinel_sources`` probe through stubs."""
    from harness.plugin_loader import plugin_module

    monkeypatch.setattr(plugin_module("sentinel.sources"), "list_sources", lambda **kw: [])
    monkeypatch.setattr(plugin_module("sentinel.agent.setup.env"), "list_sources", lambda **kw: [])
