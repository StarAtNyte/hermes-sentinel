"""GET /sources."""

from __future__ import annotations

from harness.dashboard_rig import (
    BASE,
    CAM,
    RTSP,
    no_secrets,
    watch_rule,
)
from harness.fake_sources import FakeSource
from harness.scenes import build_scene


def test_sources_empty_without_rules(client):
    assert client.get(f"{BASE}/sources").json() == []


def test_sources_online_with_frame_age(client, rig):
    watch_rule(rig)
    rig.pump(2)
    [src] = client.get(f"{BASE}/sources").json()
    assert set(src) == {"key", "label", "kind", "online", "last_frame_age_s", "rules", "error"}
    assert src["key"] == CAM and src["kind"] == "camera" and src["online"] is True
    assert src["rules"] == 1 and src["error"] is None
    assert isinstance(src["last_frame_age_s"], (int, float)) and src["last_frame_age_s"] >= 0


def test_sources_when_engine_not_running(client, rig):
    rule = watch_rule(rig)
    rig.sentinel.engine.stop()
    [src] = client.get(f"{BASE}/sources").json()
    assert src["online"] is False and src["last_frame_age_s"] is None and src["error"]
    assert client.get(f"{BASE}/rules").json()[0]["id"] == rule


def test_sources_mask_credentials(client, rig):
    rig.sources[RTSP] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock, loop=True)
    rig.sentinel.store.upsert_rule(
        rig.sentinel.store.get_rule(watch_rule(rig)).with_changes(source=RTSP)
    )
    resp = client.get(f"{BASE}/sources")
    assert resp.status_code == 200 and resp.json()
    no_secrets(resp)
    assert "***" in resp.text
    no_secrets(client.get(f"{BASE}/rules"))
    no_secrets(client.get(f"{BASE}/status"))
