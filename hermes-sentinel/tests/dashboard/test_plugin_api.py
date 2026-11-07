"""The dashboard entry point: manifest, route contract and the not-loaded behaviour."""

from __future__ import annotations

import json

import yaml
from harness.dashboard_rig import (
    BASE,
    CAM,
    load_api,
    make_client,
)
from harness.plugin_loader import PLUGIN_DIR


def test_manifest_declares_the_api():
    manifest = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())["name"]
    assert manifest["api"] == "plugin_api.py"
    assert (PLUGIN_DIR / "dashboard" / manifest["api"]).is_file()


def test_router_exports_every_contract_route(api):
    routes = {(m, r.path) for r in api.router.routes for m in r.methods}
    assert routes >= {
        ("GET", "/status"),
        ("GET", "/sources"),
        ("GET", "/frame/{source_key:path}"),
        ("GET", "/rules"),
        ("POST", "/rules/{rule_id}/pause"),
        ("POST", "/rules/{rule_id}/resume"),
        ("DELETE", "/rules/{rule_id}"),
        ("GET", "/events"),
        ("GET", "/events/{event_id}/snapshot"),
        ("POST", "/events/{event_id}/feedback"),
        ("POST", "/watch"),
    }


def test_reads_degrade_when_plugin_not_loaded(idle_client):
    status = idle_client.get("/api/plugins/hermes-sentinel/status")
    assert status.status_code == 200
    body = status.json()
    assert body["engine"] == {"running": False, "standby": False, "since": None}
    assert body["sources"] == 0 and body["rules"] == 0 and body["version"]
    assert idle_client.get(f"{BASE}/sources").json() == []
    assert idle_client.get(f"{BASE}/rules").json() == []
    assert idle_client.get(f"{BASE}/events").json() == []


def test_writes_return_503_when_plugin_not_loaded(idle_client):
    for resp in (
        idle_client.post(f"{BASE}/rules/abc/pause"),
        idle_client.delete(f"{BASE}/rules/abc"),
        idle_client.post(f"{BASE}/watch", json={"text": "x"}),
        idle_client.post(f"{BASE}/events/1/feedback", json={"verdict": "correct"}),
        idle_client.get(f"{BASE}/events/1/snapshot"),
        idle_client.get(f"{BASE}/frame/{CAM}"),
    ):
        assert resp.status_code in (404, 503), resp.text
        assert resp.json()["ok"] is False and resp.json()["error"]


def test_loaded_plugin_serves_the_real_router(api):
    from harness.plugin_loader import MODULE_NAME

    assert api.find_package() == MODULE_NAME
    paths = {r.path for r in api.router.routes}
    assert {"/status", "/rules", "/watch"} <= paths
    assert "/{path:path}" not in paths


def test_plugin_api_does_not_register_modules(api):
    import sys

    before = set(sys.modules)
    load_api("hermes_dashboard_plugin_hermes_sentinel_probe")
    added = set(sys.modules) - before
    sys.modules.pop("hermes_dashboard_plugin_hermes_sentinel_probe", None)
    assert not [name for name in added if "dashboard_plugin" not in name]


def test_without_the_plugin_every_route_answers_503(monkeypatch):
    import sys

    for name in [k for k in sys.modules if k.startswith("hermes_plugins.hermes_sentinel")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr("harness.dashboard_rig.plugin_package", lambda: None)
    name = "hermes_dashboard_plugin_hermes_sentinel_bare"
    try:
        mod = load_api(name)
    finally:
        sys.modules.pop(name, None)
    assert mod.find_package() is None
    client = make_client(mod)
    for resp in (client.get(f"{BASE}/status"), client.post(f"{BASE}/watch", json={"text": "x"})):
        assert resp.status_code == 503
        assert resp.json() == {"ok": False, "error": mod.NOT_LOADED}
