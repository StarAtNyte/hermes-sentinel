"""Finding the loaded plugin from the dashboard process."""

from __future__ import annotations

import sys

from harness.dashboard_rig import (
    BASE,
    api_module,
)
from harness.plugin_loader import plugin_module

runtime = plugin_module("sentinel.runtime")


def _fake_package(monkeypatch, name, active):
    import types

    pkg = types.ModuleType(name)
    pkg.__path__ = []
    rt = types.ModuleType(f"{name}.sentinel.runtime")
    rt.active_sentinel = lambda: active
    sent = types.ModuleType(f"{name}.sentinel")
    sent.__path__ = []
    entries = ((name, pkg), (f"{name}.sentinel", sent), (f"{name}.sentinel.runtime", rt))
    for mod_name, mod in entries:
        monkeypatch.setitem(sys.modules, mod_name, mod)


def test_scoped_package_name_is_found(api, monkeypatch):
    runtime.set_active_sentinel(None)
    marker = object()
    _fake_package(monkeypatch, "hermes_plugins.sentinel_vision__home_abc123", marker)
    assert api_module(api, "common.access").sentinel() is marker


def test_picks_the_package_with_an_active_sentinel(api, rig, monkeypatch):
    _fake_package(monkeypatch, "hermes_plugins.sentinel_vision__home_aaa", None)
    assert api_module(api, "common.access").sentinel() is rig.sentinel
    assert api_module(api, "common.access").package() == "hermes_plugins.sentinel_vision"


def test_unrelated_packages_are_ignored(api, monkeypatch):
    runtime.set_active_sentinel(None)
    _fake_package(monkeypatch, "hermes_plugins.sentinel_visionx", object())
    assert api_module(api, "common.access").sentinel() is None


def test_not_loaded_note_names_the_hosting_process(idle_client):
    resp = idle_client.get(f"{BASE}/status")
    assert "same Hermes process" in resp.json()["note"]
    resp = idle_client.post(f"{BASE}/rules/abc/pause")
    assert resp.status_code == 503 and "not loaded" in resp.json()["error"]
