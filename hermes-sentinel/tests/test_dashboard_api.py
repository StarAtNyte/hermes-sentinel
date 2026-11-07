"""The dashboard / Desktop backend (dashboard/plugin_api.py) over a fully wired Sentinel."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from harness.fake_sources import FakeSource
from harness.plugin_loader import PLUGIN_DIR, plugin_module
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

runtime = plugin_module("sentinel.runtime")
CAM = "camera:7"
RTSP = "rtsp://admin:hunter2@10.0.0.5:554/stream?token=abc123"
SECRETS = ("hunter2", "abc123")
BASE = "/api/plugins/hermes-sentinel"


def _load_api():
    """Import plugin_api.py the way Hermes does: standalone, by file path."""
    name = "hermes_dashboard_plugin_hermes-sentinel-test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def api():
    mod = _load_api()
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty desk"), task="sentinel_vision")
    runtime.set_active_sentinel(r.sentinel)
    yield r
    r.sentinel.shutdown()
    runtime.set_active_sentinel(None)


def _client(api) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


@pytest.fixture
def client(api, rig):
    return _client(api)


@pytest.fixture
def idle_client(api):
    """No Sentinel loaded in this process."""
    runtime.set_active_sentinel(None)
    return _client(api)


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((40, 60, 3), 90, np.uint8))
    assert ok
    return bytes(buf)


def _watch(rig, **args):
    args.setdefault("recipe", "desk-intruder")
    args.setdefault("source", CAM)
    return rig.watch(**args)["rule"]["rule_id"]


def _event(rig, rule_id, caption="someone at the desk", snapshot=True, **kw):
    store = rig.sentinel.store
    path = store.save_snapshot(_jpeg(), rule_id=rule_id) if snapshot else None
    return store.add_event(
        rule_id, caption=caption, confidence=0.91, source=CAM, snapshot_path=path, **kw
    )


def _no_secrets(resp):
    for secret in SECRETS:
        assert secret not in resp.text


# --------------------------------------------------------------------------- declaration


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


# --------------------------------------------------------------------------- not loaded


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


# --------------------------------------------------------------------------- status


def test_status_before_any_rule_engine_not_running(client):
    body = client.get(f"{BASE}/status").json()
    assert body["engine"]["running"] is False and body["engine"]["since"] is None
    assert body["delivery"] == {"blocked": False, "reason": None}
    assert body["budget"]["hour_used"] == 0 and body["budget"]["paused"] is False
    assert body["rules"] == 0 and body["sources"] == 0


def test_status_with_running_engine(client, rig):
    _watch(rig)
    rig.pump(1)
    body = client.get(f"{BASE}/status").json()
    assert body["engine"]["running"] is True and body["engine"]["since"]
    assert body["rules"] == 1 and body["sources"] == 1
    assert body["budget"]["hour_cap"] == 60 and body["budget"]["day_cap"] == 500


def test_status_reports_blocked_delivery(client, rig):
    alerter = rig.sentinel.alerter
    alerter._blocked = True
    body = client.get(f"{BASE}/status").json()
    assert body["delivery"]["blocked"] is True and body["delivery"]["reason"]


def test_status_budget_paused_when_cap_zero(api, fake_ctx):
    r = Rig(fake_ctx, config={"vision_calls_per_hour": 0})
    runtime.set_active_sentinel(r.sentinel)
    try:
        assert _client(api).get(f"{BASE}/status").json()["budget"]["paused"] is True
    finally:
        r.sentinel.shutdown()
        runtime.set_active_sentinel(None)


# --------------------------------------------------------------------------- sources & frames


def test_sources_empty_without_rules(client):
    assert client.get(f"{BASE}/sources").json() == []


def test_sources_online_with_frame_age(client, rig):
    _watch(rig)
    rig.pump(2)
    [src] = client.get(f"{BASE}/sources").json()
    assert set(src) == {"key", "label", "kind", "online", "last_frame_age_s", "rules", "error"}
    assert src["key"] == CAM and src["kind"] == "camera" and src["online"] is True
    assert src["rules"] == 1 and src["error"] is None
    assert isinstance(src["last_frame_age_s"], (int, float)) and src["last_frame_age_s"] >= 0


def test_sources_when_engine_not_running(client, rig):
    rule = _watch(rig)
    rig.sentinel.engine.stop()
    [src] = client.get(f"{BASE}/sources").json()
    assert src["online"] is False and src["last_frame_age_s"] is None and src["error"]
    assert client.get(f"{BASE}/rules").json()[0]["id"] == rule


def test_sources_mask_credentials(client, rig):
    rig.sources[RTSP] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock, loop=True)
    rig.sentinel.store.upsert_rule(
        rig.sentinel.store.get_rule(_watch(rig)).with_changes(source=RTSP)
    )
    resp = client.get(f"{BASE}/sources")
    assert resp.status_code == 200 and resp.json()
    _no_secrets(resp)
    assert "***" in resp.text
    _no_secrets(client.get(f"{BASE}/rules"))
    _no_secrets(client.get(f"{BASE}/status"))


def test_frame_is_jpeg_no_store_and_bounded(client, rig):
    _watch(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/{CAM}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "no-store" in resp.headers["cache-control"]
    img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[1] <= 640


def test_frame_unknown_source_is_404_json(client, rig):
    _watch(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/camera:99")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_frame_without_running_engine_is_404(client):
    resp = client.get(f"{BASE}/frame/{CAM}")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_frame_never_opens_a_device(client, rig):
    _watch(rig)
    opened = list(rig.opened)
    client.get(f"{BASE}/frame/camera:99")
    client.get(f"{BASE}/frame/{CAM}")
    assert rig.opened == opened


def test_frame_overlong_key_rejected(client, rig):
    _watch(rig)
    resp = client.get(f"{BASE}/frame/" + "a" * 600)
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_frame_with_credentialed_source_masks_key_and_resolves(client, rig):
    rig.sources[RTSP] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock, loop=True)
    store = rig.sentinel.store
    spec = store.upsert_rule(store.get_rule(_watch(rig)).with_changes(source=RTSP))
    rig.engine.update_rule(spec)
    rig.pump(1)
    [src] = client.get(f"{BASE}/sources").json()
    _no_secrets(client.get(f"{BASE}/sources"))
    assert src["key"] != RTSP and "***" in src["key"]
    resp = client.get(f"{BASE}/frame/" + quote(src["key"], safe=""))
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert client.get(f"{BASE}/frame/{RTSP}").status_code == 404  # raw key is not addressable


# --------------------------------------------------------------------------- rules


def test_rules_list_shape(client, rig):
    rule = _watch(rig)
    rig.pump(1)
    [item] = client.get(f"{BASE}/rules").json()
    assert set(item) == {
        "id",
        "text",
        "source",
        "mode",
        "status",
        "paused",
        "last_event_at",
        "vision_calls_hour",
        "cooldown_s",
        "hold_s",
    }
    assert item["id"] == rule and item["source"] == CAM and item["paused"] is False
    assert item["last_event_at"] is None and item["vision_calls_hour"] >= 0
    assert item["text"] and item["mode"] in ("trigger", "condition", "absence")


def test_rules_list_shows_last_event(client, rig):
    rule = _watch(rig)
    ev = _event(rig, rule)
    assert client.get(f"{BASE}/rules").json()[0]["last_event_at"] == ev.ts


def test_pause_resume_delete_roundtrip(client, rig):
    rule = _watch(rig)
    paused = client.post(f"{BASE}/rules/{rule}/pause")
    assert paused.status_code == 200 and paused.json()["ok"] is True
    assert paused.json()["rule"]["paused"] is True and paused.json()["rule"]["status"] == "paused"
    assert rig.sentinel.store.get_rule(rule).enabled is False
    resumed = client.post(f"{BASE}/rules/{rule}/resume").json()
    assert resumed["rule"]["paused"] is False
    assert rig.sentinel.store.get_rule(rule).enabled is True
    deleted = client.delete(f"{BASE}/rules/{rule}")
    assert deleted.status_code == 200 and deleted.json()["rule"]["id"] == rule
    assert rig.sentinel.store.get_rule(rule) is None
    assert client.get(f"{BASE}/rules").json() == []


def test_delete_keeps_events(client, rig):
    rule = _watch(rig)
    _event(rig, rule)
    client.delete(f"{BASE}/rules/{rule}")
    assert len(client.get(f"{BASE}/events").json()) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/rules/nope/pause"),
        ("post", "/rules/nope/resume"),
        ("delete", "/rules/nope"),
    ],
)
def test_unknown_rule_is_404(client, method, path):
    resp = getattr(client, method)(BASE + path)
    assert resp.status_code == 404 and resp.json()["ok"] is False


@pytest.mark.parametrize("bad", ["a b", "x" * 70, "a.b", "%24%7Bx%7D"])
def test_invalid_rule_id_is_400(client, bad):
    resp = client.post(f"{BASE}/rules/{bad}/pause")
    assert resp.status_code in (400, 404) and resp.json()["ok"] is False


def test_dashboard_owns_rules_made_in_messaging_chats(client, rig):
    rule = _watch(rig)
    spec = rig.sentinel.store.get_rule(rule)
    rig.sentinel.store.upsert_rule(
        spec.with_changes(origin={"platform": "telegram", "chat_id": "42"})
    )
    assert [r["id"] for r in client.get(f"{BASE}/rules").json()] == [rule]
    assert client.post(f"{BASE}/rules/{rule}/pause").status_code == 200


# --------------------------------------------------------------------------- events


def test_events_list_shape_and_order(client, rig):
    rule = _watch(rig)
    first = _event(rig, rule, caption="first", snapshot=False)
    second = _event(rig, rule, caption="second")
    items = client.get(f"{BASE}/events").json()
    assert [i["id"] for i in items] == [second.id, first.id]
    top = items[0]
    assert set(top) == {
        "id",
        "rule_id",
        "rule_text",
        "ts",
        "caption",
        "confidence",
        "feedback",
        "has_snapshot",
    }
    assert top["has_snapshot"] is True and items[1]["has_snapshot"] is False
    assert top["feedback"] is None and top["rule_text"] and top["confidence"] == 0.91


def test_events_filters_and_limits(client, rig):
    a, b = _watch(rig), _watch(rig, recipe="desk-intruder")
    for i in range(5):
        _event(rig, a, caption=f"a{i}", snapshot=False)
    _event(rig, b, caption="b0", snapshot=False)
    assert len(client.get(f"{BASE}/events?limit=3").json()) == 3
    assert {e["rule_id"] for e in client.get(f"{BASE}/events?rule_id={b}").json()} == {b}
    assert len(client.get(f"{BASE}/events?limit=100000").json()) == 6  # clamped, not an error
    assert client.get(f"{BASE}/events?since=1h").status_code == 200
    assert client.get(f"{BASE}/events?since=2999-01-01T00:00:00Z").json() == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=abc",
        "limit=0",
        "limit=-1",
        "rule_id=bad id!",
        "since=whenever",
    ],
)
def test_events_bad_params_are_400(client, rig, query):
    resp = client.get(f"{BASE}/events?{query}")
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_snapshot_returns_jpeg(client, rig):
    ev = _event(rig, _watch(rig))
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(b"\xff\xd8")


def test_snapshot_errors(client, rig):
    rule = _watch(rig)
    assert client.get(f"{BASE}/events/9999/snapshot").status_code == 404
    assert client.get(f"{BASE}/events/abc/snapshot").status_code == 400
    bare = _event(rig, rule, snapshot=False)
    assert client.get(f"{BASE}/events/{bare.id}/snapshot").status_code == 404
    gone = _event(rig, rule)
    Path(rig.sentinel.store.get_event(gone.id).snapshot_path).unlink()
    resp = client.get(f"{BASE}/events/{gone.id}/snapshot")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_feedback_correct_and_false_alarm(client, rig):
    rule = _watch(rig)
    ev = _event(rig, rule)
    before = rig.sentinel.store.get_rule(rule).confidence_threshold
    ok = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "correct"}).json()
    assert ok["ok"] is True and ok["event"]["feedback"] == "correct"
    lowered = before - 0.01  # a confirmed alert nudges the threshold down a little
    assert ok["threshold"] == pytest.approx(lowered)
    bad = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "false_alarm"}).json()
    assert bad["event"]["feedback"] == "false_alarm"
    assert bad["threshold"] == pytest.approx(lowered + 0.05)
    assert rig.sentinel.store.get_rule(rule).confidence_threshold == pytest.approx(lowered + 0.05)
    again = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "false_alarm"}).json()
    assert again["threshold"] == pytest.approx(lowered + 0.05)  # idempotent


@pytest.mark.parametrize("payload", [{}, {"verdict": "maybe"}, {"verdict": 1}, [], "x"])
def test_feedback_bad_body_is_400(client, rig, payload):
    ev = _event(rig, _watch(rig))
    resp = client.post(f"{BASE}/events/{ev.id}/feedback", json=payload)
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_feedback_malformed_and_oversize_bodies(client, rig):
    ev = _event(rig, _watch(rig))
    url = f"{BASE}/events/{ev.id}/feedback"
    assert client.post(url, content=b"{nope").status_code == 400
    assert client.post(url, json={"verdict": "correct", "pad": "x" * 20000}).status_code == 413


def test_feedback_unknown_event(client, rig):
    resp = client.post(f"{BASE}/events/9999/feedback", json={"verdict": "correct"})
    assert resp.status_code == 404 and resp.json()["ok"] is False
    assert client.post(f"{BASE}/events/x/feedback", json={"verdict": "correct"}).status_code == 400


# --------------------------------------------------------------------------- watch


def test_watch_with_recipe(client, rig):
    resp = client.post(
        f"{BASE}/watch", json={"text": "my desk", "source": CAM, "recipe": "desk-intruder"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["rule"]["source"] == CAM
    assert body["rule"]["id"] and rig.sentinel.store.get_rule(body["rule"]["id"])
    assert "note" in body and "Alerts are injected" not in body["note"]
    assert rig.sentinel.engine.running


def test_watch_plain_text_uses_compiler(client, rig):
    rig.ctx.llm.respond(
        lambda call: {"question": "Is a mug on the desk?", "mode": "absence"}, task="sentinel_rules"
    )
    resp = client.post(f"{BASE}/watch", json={"text": "tell me if the mug is gone", "source": CAM})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rule"]["text"]


@pytest.mark.parametrize(
    "payload,status",
    [
        ({}, 400),
        ({"text": ""}, 400),
        ({"text": 5}, 400),
        ({"text": "x" * 2500}, 400),
        ({"text": "x", "recipe": "no-such-recipe"}, 400),
        ({"text": "x", "source": "bogus://"}, 400),
        ([], 400),
    ],
)
def test_watch_validation(client, rig, payload, status):
    resp = client.post(f"{BASE}/watch", json=payload)
    assert resp.status_code == status and resp.json()["ok"] is False


def test_watch_errors_never_leak_credentials(client, rig):
    resp = client.post(f"{BASE}/watch", json={"text": "x", "source": RTSP, "recipe": "nope"})
    assert resp.status_code == 400
    _no_secrets(resp)


# --------------------------------------------------------------------------- data_url encoding


def _decode_data_url(url: str) -> bytes:
    import base64

    head, _, b64 = url.partition(",")
    assert head == "data:image/jpeg;base64"
    return base64.b64decode(b64)


def test_frame_data_url_encoding(client, rig):
    _watch(rig)
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
    ev = _event(rig, _watch(rig))
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=data_url")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _decode_data_url(resp.json()["data_url"]).startswith(b"\xff\xd8")


def test_data_url_errors_stay_json_404(client, rig):
    _watch(rig)
    rig.pump(1)
    resp = client.get(f"{BASE}/frame/camera:99?encoding=data_url")
    assert resp.status_code == 404 and resp.json()["ok"] is False
    resp = client.get(f"{BASE}/events/9999/snapshot?encoding=data_url")
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_data_url_unknown_encoding_and_size_bound(client, rig, api, monkeypatch):
    ev = _event(rig, _watch(rig))
    assert client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=base64").status_code == 400
    monkeypatch.setattr(api, "MAX_DATA_URL_BYTES", 10)
    resp = client.get(f"{BASE}/events/{ev.id}/snapshot?encoding=data_url")
    assert resp.status_code == 413 and resp.json()["ok"] is False
    # raw path is not subject to the inline bound
    assert client.get(f"{BASE}/events/{ev.id}/snapshot").status_code == 200


# --------------------------------------------------------------------------- package lookup


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
    _fake_package(monkeypatch, "hermes_plugins.hermes_sentinel__home_abc123", marker)
    assert api._sentinel() is marker


def test_picks_the_package_with_an_active_sentinel(api, rig, monkeypatch):
    _fake_package(monkeypatch, "hermes_plugins.hermes_sentinel__home_aaa", None)
    assert api._sentinel() is rig.sentinel
    assert api._package() == "hermes_plugins.hermes_sentinel"


def test_unrelated_packages_are_ignored(api, monkeypatch):
    runtime.set_active_sentinel(None)
    _fake_package(monkeypatch, "hermes_plugins.hermes_sentinelx", object())
    assert api._sentinel() is None


def test_not_loaded_note_names_the_hosting_process(idle_client):
    resp = idle_client.get(f"{BASE}/status")
    assert "same Hermes process" in resp.json()["note"]
    resp = idle_client.post(f"{BASE}/rules/abc/pause")
    assert resp.status_code == 503 and "not loaded" in resp.json()["error"]


# --------------------------------------------------------------------------- masking shapes


@pytest.mark.parametrize(
    "spec,secret",
    [
        ("snapshot:https://u:pw@h.example/x.jpg", "pw"),
        ("https://tok123@h.example/x", "tok123"),
        ("folder:https://tok123@h.example/x", "tok123"),
        ("rtsp://h.example/live/SECRETPATH", "SECRETPATH"),
        ("rtsp://h.example:554/Streaming/s3cr3t-tok/ch1?x=1", "s3cr3t-tok"),
    ],
)
def test_mask_hides_credential_shapes(api, rig, spec, secret):
    out = api._mask(spec)
    assert secret not in out
    assert "://" in out


def test_mask_keeps_placeholders_and_generic_paths(api, rig):
    assert api._mask("https://${CAM_USER}@h.example/x") == "https://${CAM_USER}@h.example/x"
    assert api._mask("snapshot:https://u:${CAM_PW}@h.example/x.jpg").startswith("snapshot:https://")
    assert "${CAM_PW}" in api._mask("snapshot:https://u:${CAM_PW}@h.example/x.jpg")
    assert api._mask("rtsp://nvr/ch1") == "rtsp://nvr/ch1"
    assert api._mask("camera:0") == "camera:0"
    # two cameras behind one recorder stay distinguishable
    a = api._mask("rtsp://nvr/secretAAAA")
    b = api._mask("rtsp://nvr/secretBBBB")
    assert a != b
