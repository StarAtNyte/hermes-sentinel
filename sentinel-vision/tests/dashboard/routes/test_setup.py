"""GET /setup."""

from __future__ import annotations

import json

from harness.plugin_loader import plugin_module

env_mod = plugin_module("sentinel.agent.setup.env")


cfg_mod = plugin_module("sentinel.config")


runtime = plugin_module("sentinel.runtime")


BASE = "/api/plugins/sentinel-vision"


def test_api_setup_does_not_probe_unless_asked(client, rig, monkeypatch, no_real_device_probe):
    probes = []
    monkeypatch.setattr(env_mod, "_probe", lambda **kw: probes.append(kw) or [])
    monkeypatch.setattr(env_mod, "list_sources", lambda **kw: probes.append(kw) or [])
    body = client.get(f"{BASE}/setup").json()
    assert body["ok"] is True and body["probed"] is False and probes == []
    assert body["ready"] is True and body["summary"].startswith("Ready: yes")
    assert body["checks"][0]["id"] == "deps"
    assert all(set(c) == {"id", "title", "status", "detail", "fix"} for c in body["checks"])
    resp = client.get(f"{BASE}/setup?probe=1")
    assert resp.json()["probed"] is True
    assert resp.headers["cache-control"] == "no-store"


def test_api_setup_masks_credentials(client, rig, no_real_device_probe):
    rig.sentinel.config = cfg_mod.SentinelConfig(default_source="rtsp://admin:hunter2@h/s")
    body = client.get(f"{BASE}/setup").json()
    assert "hunter2" not in json.dumps(body)
    assert next(c for c in body["checks"] if c["id"] == "default_source")["status"] == "fail"


def test_api_setup_when_plugin_not_loaded(client, no_real_device_probe):
    runtime.set_active_sentinel(None)
    resp = client.get(f"{BASE}/setup")
    assert resp.status_code == 503 and resp.json()["ok"] is False
