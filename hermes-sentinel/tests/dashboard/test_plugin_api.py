"""The dashboard entry point: manifest, route contract and the not-loaded behaviour."""

from __future__ import annotations

import json

import yaml
from harness.dashboard_rig import (
    BASE,
    CAM,
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
