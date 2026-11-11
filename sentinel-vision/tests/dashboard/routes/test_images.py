"""GET /frame and GET /events/{id}/snapshot, raw and as data URLs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from harness.dashboard_rig import (
    BASE,
    CAM,
    RTSP,
    add_event,
    api_module,
    no_secrets,
    watch_rule,
)
from harness.fake_sources import FakeSource
from harness.scenes import build_scene


def _decode_data_url(url: str) -> bytes:
    import base64

    head, _, b64 = url.partition(",")
    assert head == "data:image/jpeg;base64"
    return base64.b64decode(b64)


def test_frame_is_jpeg_no_store_and_bounded(client, rig):
    watch_rule(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/{CAM}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "no-store" in resp.headers["cache-control"]
    img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[1] <= 640


def test_frame_unknown_source_is_404_json(client, rig):
    watch_rule(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/camera:99")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_frame_without_running_engine_is_404(client):
    resp = client.get(f"{BASE}/frame/{CAM}")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_frame_never_opens_a_device(client, rig):
    watch_rule(rig)
    opened = list(rig.opened)
    client.get(f"{BASE}/frame/camera:99")
    client.get(f"{BASE}/frame/{CAM}")
    assert rig.opened == opened


def test_frame_overlong_key_rejected(client, rig):
    watch_rule(rig)
    resp = client.get(f"{BASE}/frame/" + "a" * 600)
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_frame_with_credentialed_source_masks_key_and_resolves(client, rig):
    rig.sources[RTSP] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock, loop=True)
    store = rig.sentinel.store
    spec = store.upsert_rule(store.get_rule(watch_rule(rig)).with_changes(source=RTSP))
    rig.engine.update_rule(spec)
    rig.pump(1)
    [src] = client.get(f"{BASE}/sources").json()
    no_secrets(client.get(f"{BASE}/sources"))
    assert src["key"] != RTSP and "***" in src["key"]
    resp = client.get(f"{BASE}/frame/" + quote(src["key"], safe=""))
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert client.get(f"{BASE}/frame/{RTSP}").status_code == 404  # raw key is not addressable


def test_snapshot_returns_jpeg(client, rig):
    ev = add_event(rig, watch_rule(rig))
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(b"\xff\xd8")


def test_snapshot_errors(client, rig):
    rule = watch_rule(rig)
    assert client.get(f"{BASE}/events/9999/snapshot").status_code == 404
    assert client.get(f"{BASE}/events/abc/snapshot").status_code == 400
    bare = add_event(rig, rule, snapshot=False)
    assert client.get(f"{BASE}/events/{bare.id}/snapshot").status_code == 404
    gone = add_event(rig, rule)
    Path(rig.sentinel.store.get_event(gone.id).snapshot_path).unlink()
    resp = client.get(f"{BASE}/events/{gone.id}/snapshot")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_frame_data_url_encoding(client, rig):
    watch_rule(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/{CAM}", params={"encoding": "data_url"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "no-store" in resp.headers["cache-control"]
    body = resp.json()
    assert body["ok"] is True
    raw = _decode_data_url(body["data_url"])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[1] <= 640
    # without the parameter the raw JPEG contract is unchanged
    assert client.get(f"{BASE}/frame/{CAM}").headers["content-type"] == "image/jpeg"


def test_snapshot_data_url_encoding(client, rig):
    ev = add_event(rig, watch_rule(rig))
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=data_url")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _decode_data_url(resp.json()["data_url"]).startswith(b"\xff\xd8")


def test_data_url_errors_stay_json_404(client, rig):
    watch_rule(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/camera:99?encoding=data_url")
    assert resp.status_code == 404 and resp.json()["ok"] is False
    resp = client.get(f"{BASE}/events/9999/snapshot?encoding=data_url")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_data_url_unknown_encoding_and_size_bound(client, rig, api, monkeypatch):
    ev = add_event(rig, watch_rule(rig))
    assert client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=base64").status_code == 400
    monkeypatch.setattr(api_module(api, "routes.images"), "MAX_DATA_URL_BYTES", 10)
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=data_url")
    assert resp.status_code == 413 and resp.json()["ok"] is False
    # raw path is not subject to the inline bound
    assert client.get(f"{BASE}/events/{ev.id}/snapshot").status_code == 200
