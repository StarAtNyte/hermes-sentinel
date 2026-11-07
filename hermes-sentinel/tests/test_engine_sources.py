"""Engine behaviour for finite sources (video files) and sources waiting for the secret scope."""

from __future__ import annotations

from harness.plugin_loader import plugin_module
from sentinel_rig import Rig
from test_engine import Harness, h  # noqa: F401  (h is the fixture)

sources_mod = plugin_module("sentinel.sources")
secrets_mod = plugin_module("sentinel.sources.spec.secrets")


def test_finished_video_stops_without_reconnecting(h):  # noqa: F811
    h.add()
    h.sources["cam"].read_error = sources_mod.SourceExhausted("reached the end of clip.mp4")
    assert h.engine.step_source("cam") == "finished"
    assert len(h.notices("finished")) == 1
    opened = h.sources["cam"].open_count
    for _ in range(30):
        h.clock.advance(60)
        assert h.engine.step_source("cam") == "waiting"
    assert h.sources["cam"].open_count == opened
    assert h.engine.status()["sources"][0]["online"] is False
    assert h.notices("offline") == []


def test_waiting_for_secret_scope_is_not_an_outage(h):  # noqa: F811
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("drop")
    h.engine.step_source("cam")
    src.read_error = None
    src.open_error = sources_mod.SecretScopeUnavailable("waiting for secret scope: 'X'")
    for _ in range(10):
        h.clock.advance(6)
        assert h.engine.step_source("cam") == "offline"
    assert h.notices("offline") == []
    status = h.engine.status()["sources"][0]
    assert status["online"] is False and status["error"].startswith("waiting for secret scope")
    src.open_error = None
    h.clock.advance(6)
    assert h.engine.step_source("cam") == "frame"
    assert h.engine.status()["sources"][0]["online"] is True


def test_waiting_source_retries_within_seconds_not_backoff_minutes(h):  # noqa: F811
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("drop")
    h.engine.step_source("cam")
    src.read_error = None
    src.open_error = sources_mod.SecretScopeUnavailable("waiting for secret scope: 'X'")
    for _ in range(12):
        h.clock.advance(9)
        h.engine.step_source("cam")
    src.open_error = None
    h.clock.advance(6)
    assert h.engine.step_source("cam") == "frame"


def test_tool_call_primes_the_cache_for_later_scopeless_threads(fake_ctx, shim_binding):
    shim_binding.secrets["CAM_PW"] = "pw"
    rig = Rig(fake_ctx, config={"stream_urls": {"door": "rtsp://u:${CAM_PW}@10.0.0.5/live"}})
    secrets_mod.SECRET_CACHE.clear()
    rig.call("sentinel_rules")  # any tool call runs inside the scope
    assert secrets_mod.SECRET_CACHE.recall("CAM_PW") == "pw"
    rig.sentinel.shutdown()
    assert secrets_mod.SECRET_CACHE.recall("CAM_PW") is None


def test_rule_sources_are_primed_too(fake_ctx, shim_binding):
    shim_binding.secrets["CAM_PW"] = "pw"
    rig = Rig(fake_ctx, {"rtsp://u:${CAM_PW}@h/x": object()})
    rig.sentinel.store.upsert_rule(_rule("rtsp://u:${CAM_PW}@h/x"))
    secrets_mod.SECRET_CACHE.clear()
    rig.sentinel.prime_secrets()
    assert secrets_mod.SECRET_CACHE.recall("CAM_PW") == "pw"


def _rule(source: str):
    rules = plugin_module("sentinel.rules")
    return rules.RuleSpec(question="anyone?", source=source, id="r1").validate()
