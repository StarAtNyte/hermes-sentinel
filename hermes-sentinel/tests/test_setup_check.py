"""Setup check: every check with fakes, the /sentinel command, the tool, the watch hint, the API."""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from harness.fake_sources import FakeSource
from harness.plugin_loader import PLUGIN_DIR, plugin_module
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

sc = plugin_module("sentinel.setup_check")
cfg_mod = plugin_module("sentinel.config")
sources_mod = plugin_module("sentinel.sources")
runtime = plugin_module("sentinel.runtime")
BASE = "/api/plugins/hermes-sentinel"
CAM = "camera:7"


def info(spec, kind="camera", available=True, name="Cam"):
    return sources_mod.SourceInfo(spec=spec, kind=kind, name=name, available=available)


class FakeStore:
    def __init__(self, data_dir, rules=()):
        self.data_dir = data_dir
        self._rules = list(rules)

    def list_rules(self, **_):
        return self._rules


class FakeAlerter:
    def __init__(self, blocked=None, sent=0):
        self._blocked, self._sent = blocked, sent

    def delivery_blocked(self):
        return self._blocked

    def stats(self):
        return {"sent": self._sent, "suppressed": 0, "failed": 0}


def make_env(tmp_path, **over):
    values = dict(
        config=cfg_mod.SentinelConfig(),
        has_llm=True,
        store_getter=lambda: FakeStore(tmp_path),
        engine=None,
        alerter=FakeAlerter(),
        route=None,
        module_available=lambda name: True,
        list_sources=lambda **kw: [info("camera:0"), info("screen:1", "screen", name="Monitor")],
        disk_free_mb=lambda path: 10_000.0,
    )
    values.update(over)
    return sc.SetupEnv(**values)


def run(tmp_path, **over):
    return {c.id: c for c in sc.run_checks(make_env(tmp_path, **over))}


def test_healthy_setup_has_only_unverifiable_warnings(tmp_path):
    checks = sc.run_checks(make_env(tmp_path))
    assert [c.id for c in checks] == [
        "deps",
        "injection",
        "vision",
        "sources",
        "default_source",
        "engine",
        "storage",
        "delivery",
        "budget",
        "rules",
    ]
    assert {c.id for c in checks if c.status != "ok"} == {"injection", "vision"}
    assert sc.summarize(checks)["ready"] is True
    assert sc.format_report(checks).splitlines()[-1] == "Ready: yes - 2 items to review"


def _last_line(*statuses: str) -> str:
    checks = [sc.Check(id=str(i), title=str(i), status=s) for i, s in enumerate(statuses)]
    return sc.format_report(checks).splitlines()[-1]


def test_ready_line_wording():
    assert _last_line("ok") == "Ready: yes - nothing to fix"
    assert _last_line("ok", "warn") == "Ready: yes - 1 item to review"
    assert _last_line("warn", "fail") == "Ready: no - 2 things to fix"
    assert _last_line("fail") == "Ready: no - 1 thing to fix"


def test_deps_missing(tmp_path):
    missing = run(tmp_path, module_available=lambda m: m not in ("numpy", "cv2"))["deps"]
    assert missing.status == "fail" and "numpy" in missing.detail
    assert "opencv-python-headless" in missing.detail
    assert "opencv-python-headless" in missing.fix and "enable" in missing.fix
    mss = run(tmp_path, module_available=lambda m: m != "mss")["deps"]
    assert mss.status == "warn" and "screen capture" in mss.detail


def test_injection_unknown_proven_and_blocked(tmp_path):
    unknown = run(tmp_path)["injection"]
    assert unknown.status == "warn" and "allow_gateway_injection: true" in unknown.fix
    assert "Cannot be verified" in unknown.detail
    assert run(tmp_path, alerter=FakeAlerter(sent=2))["injection"].status == "ok"
    blocked = run(tmp_path, alerter=FakeAlerter(blocked="refused"))["injection"]
    assert blocked.status == "fail" and "allow_gateway_injection: true" in blocked.fix


def test_vision_model_is_never_claimed_ok(tmp_path):
    c = run(tmp_path)["vision"]
    assert c.status == "warn" and "must accept images" in c.detail
    assert "default model" in c.detail and "sentinel_vision" in c.fix
    assert run(tmp_path, has_llm=False)["vision"].status == "fail"


def test_no_camera_found(tmp_path):
    c = run(tmp_path, list_sources=lambda **kw: [])["sources"]
    assert c.status == "warn" and "No camera" in c.detail
    off = run(tmp_path, list_sources=lambda **kw: [info("camera:0", available=False)])["sources"]
    assert off.status == "warn"


def test_sources_found_configured_and_not_probed(tmp_path):
    found = run(tmp_path)["sources"]
    assert found.status == "ok" and "camera:0" in found.detail and "screen:1" in found.detail
    stream = run(tmp_path, list_sources=lambda **kw: [info("porch", "stream", None, "porch")])[
        "sources"
    ]
    assert stream.status == "ok" and "porch (stream)" in stream.detail

    def boom(**kw):
        raise AssertionError("must not probe")

    skipped = run(tmp_path, probe_sources=False, list_sources=boom)["sources"]
    assert skipped.status == "ok" and "not probed" in skipped.detail

    def crash(**kw):
        raise RuntimeError("driver")

    crashed = run(tmp_path, list_sources=crash)["sources"]
    assert crashed.status == "warn" and "RuntimeError" in crashed.detail


def test_probe_uses_short_timeouts(tmp_path):
    seen = {}

    def probe(**kw):
        seen.update(kw)
        return []

    run(tmp_path, list_sources=probe)
    assert seen["total_timeout"] <= 5 and seen["per_camera_timeout"] <= 2


@pytest.mark.parametrize(
    "source, status",
    [
        ("camera:0", "ok"),
        ("screen", "ok"),
        ("rtsp://user:pw@h/x", "fail"),
        ("nonsense://x", "fail"),
    ],
)
def test_default_source_validity(tmp_path, source, status):
    cfg = cfg_mod.SentinelConfig(default_source=source)
    c = run(tmp_path, config=cfg, list_sources=lambda **kw: [])["default_source"]
    assert c.status == status, c.detail
    if status == "fail":
        assert "pw" not in c.detail  # credentials never echoed


def test_default_source_not_among_found_devices(tmp_path):
    cfg = cfg_mod.SentinelConfig(default_source="camera:3")
    c = run(tmp_path, config=cfg)["default_source"]
    assert c.status == "warn" and "camera:0" in c.detail


def test_config_problems_listed_only_when_present(tmp_path):
    assert "config" not in run(tmp_path)
    cfg = cfg_mod.SentinelConfig(problems=("confirm_k: nope",))
    assert run(tmp_path, config=cfg)["config"].status == "warn"


def test_engine_states(tmp_path):
    assert run(tmp_path, engine=SimpleNamespace(running=True))["engine"].detail == "Running."
    assert (
        "Standing by"
        in run(tmp_path, engine=SimpleNamespace(running=False, standing_by=True))["engine"].detail
    )
    assert "Not started" in run(tmp_path)["engine"].detail


def test_storage_writable_low_and_unwritable(tmp_path):
    ok = run(tmp_path)["storage"]
    assert ok.status == "ok" and "GB free" in ok.detail
    assert list(tmp_path.iterdir()) == []
    assert run(tmp_path, disk_free_mb=lambda p: 20.0)["storage"].status == "fail"
    assert run(tmp_path, disk_free_mb=lambda p: 200.0)["storage"].status == "warn"
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = run(tmp_path, store_getter=lambda: FakeStore(blocker / "sub"))["storage"]
    assert bad.status == "fail" and "Cannot open or write" in bad.detail

    def nostore():
        raise OSError("no db")

    assert run(tmp_path, store_getter=nostore)["storage"].status == "fail"

    def nodisk(path):
        raise OSError("x")

    assert run(tmp_path, disk_free_mb=nodisk)["storage"].status == "ok"


def test_delivery_route(tmp_path):
    assert run(tmp_path, route=None)["delivery"].status == "ok"
    known = run(tmp_path, route={"session_key": "k", "platform": "telegram"})["delivery"]
    assert known.status == "ok"
    unknown = run(tmp_path, route={})["delivery"]
    assert unknown.status == "warn" and "/sentinel here" in unknown.fix


def test_budget_caps(tmp_path):
    def with_caps(**kw):
        return run(tmp_path, config=cfg_mod.SentinelConfig(**kw))["budget"]

    assert with_caps().status == "ok"
    assert with_caps(vision_calls_per_hour=0).status == "fail"
    assert with_caps(vision_calls_per_hour=None).status == "warn"
    assert with_caps(vision_calls_per_hour=100, vision_calls_per_day=50).status == "warn"
    assert with_caps(vision_calls_per_hour=900, vision_calls_per_day=9000).status == "warn"


def test_rules_count_and_offline_source(tmp_path):
    assert "No rules yet" in run(tmp_path)["rules"].detail
    rules = [SimpleNamespace(id="r1", enabled=True), SimpleNamespace(id="r2", enabled=False)]
    engine = SimpleNamespace(
        running=True,
        standing_by=False,
        status=lambda: {"sources": [{"source": "camera:3", "online": False, "rules": ["r1"]}]},
    )
    c = run(tmp_path, store_getter=lambda: FakeStore(tmp_path, rules), engine=engine)["rules"]
    assert c.status == "warn" and "2 rule(s), 1 active" in c.detail
    assert "r1 (camera:3)" in c.detail
    idle = SimpleNamespace(running=False, standing_by=False)
    waiting = run(tmp_path, store_getter=lambda: FakeStore(tmp_path, rules), engine=idle)["rules"]
    assert waiting.status == "warn" and "not running" in waiting.detail


def test_a_crashing_check_is_contained(tmp_path):
    def boom():
        raise RuntimeError("x")

    checks = {c.id: c for c in sc.run_checks(make_env(tmp_path, store_getter=boom))}
    assert checks["rules"].status == "warn" and "RuntimeError" in checks["rules"].detail
    assert checks["storage"].status == "fail"


def test_summary_and_titles(tmp_path):
    checks = sc.run_checks(
        make_env(
            tmp_path,
            module_available=lambda m: False,
            config=cfg_mod.SentinelConfig(vision_calls_per_hour=0),
        )
    )
    assert sc.fail_titles(checks) == ["Python packages", "Vision call caps"]
    s = sc.summarize(checks)
    assert s["ready"] is False and s["failed"] == 2
    assert sc.format_report(checks).splitlines()[-1].startswith("Ready: no - ")
    assert [c.to_dict()["status"] for c in checks][0] == "fail"


# --------------------------------------------------------------------------- live wiring


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty"), task="sentinel_vision")
    runtime.set_active_sentinel(r.sentinel)
    yield r
    r.sentinel.shutdown()
    runtime.set_active_sentinel(None)


@pytest.fixture(autouse=True)
def no_real_device_probe(monkeypatch):
    """Never touch real cameras: SetupEnv's default probe goes through sources.list_sources."""
    monkeypatch.setattr(sources_mod, "list_sources", lambda **kw: [])
    monkeypatch.setattr(sc, "list_sources", lambda **kw: [])


def test_command_setup(rig):
    out = rig.ctx.run_command("sentinel", "setup")
    assert out.startswith("Sentinel setup check")
    assert "Cameras and screens: No camera" in out
    assert out.splitlines()[-1].startswith("Ready: yes - ")
    assert "setup [--test-delivery] |" in rig.ctx.run_command("sentinel", "help")


def test_tool_setup_check(rig):
    out = rig.call("sentinel_setup_check")
    assert out["ok"] is True and out["ready"] is True
    assert [c["id"] for c in out["checks"]][:2] == ["deps", "injection"]
    assert out["report"].startswith("Sentinel setup check")
    schema = plugin_module("sentinel.tools").SCHEMAS["sentinel_setup_check"]
    assert set(schema["parameters"]["properties"]) == {"test_delivery"}


def test_watch_mentions_setup_only_when_a_check_fails(rig):
    ok = rig.watch(recipe="desk-intruder", source=CAM)
    assert "Setup problem" not in ok["message"]
    rig.sentinel.config = cfg_mod.SentinelConfig(vision_calls_per_hour=0)
    bad = rig.watch(recipe="desk-intruder", source=CAM)
    assert "Setup problem (Vision call caps)" in bad["message"]
    assert "/sentinel setup" in bad["message"]


# --------------------------------------------------------------------------- API


@pytest.fixture
def client():
    name = "hermes_dashboard_plugin_hermes-sentinel-setup-test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix=BASE)
    yield TestClient(app)
    sys.modules.pop(name, None)


def test_api_setup_does_not_probe_unless_asked(client, rig, monkeypatch):
    probes = []
    monkeypatch.setattr(sc, "_probe", lambda **kw: probes.append(kw) or [])
    monkeypatch.setattr(sc, "list_sources", lambda **kw: probes.append(kw) or [])
    body = client.get(f"{BASE}/setup").json()
    assert body["ok"] is True and body["probed"] is False and probes == []
    assert body["ready"] is True and body["summary"].startswith("Ready: yes")
    assert body["checks"][0]["id"] == "deps"
    assert all(set(c) == {"id", "title", "status", "detail", "fix"} for c in body["checks"])
    resp = client.get(f"{BASE}/setup?probe=1")
    assert resp.json()["probed"] is True
    assert resp.headers["cache-control"] == "no-store"


def test_api_setup_masks_credentials(client, rig):
    rig.sentinel.config = cfg_mod.SentinelConfig(default_source="rtsp://admin:hunter2@h/s")
    body = client.get(f"{BASE}/setup").json()
    assert "hunter2" not in json.dumps(body)
    assert next(c for c in body["checks"] if c["id"] == "default_source")["status"] == "fail"


def test_api_setup_when_plugin_not_loaded(client):
    runtime.set_active_sentinel(None)
    resp = client.get(f"{BASE}/setup")
    assert resp.status_code == 503 and resp.json()["ok"] is False
