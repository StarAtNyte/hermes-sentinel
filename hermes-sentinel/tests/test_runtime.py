"""The Sentinel service holder: storage failure, shutdown and boot."""

from __future__ import annotations

import pytest
from harness.fake_sources import FakeSource
from harness.scenes import build_scene
from sentinel_rig import Rig, runtime, verdict

CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(
        lambda call: verdict(True, 0.85, "someone at the desk"), task="sentinel_vision"
    )
    return r


def add_rule(rig):
    return rig.watch(recipe="desk-intruder", source=CAM)["rule"]["rule_id"]


def runtime_status(sentinel):
    from harness.plugin_loader import plugin_module

    return plugin_module("sentinel.agent.commands").SentinelCommand(sentinel).status()


def test_store_failure_is_friendly(fake_ctx):
    def broken(cfg):
        raise runtime.StoreError("disk is read-only")

    s = runtime.Sentinel(fake_ctx, store_factory=broken, threaded=False)
    s.boot()  # logs, does not raise
    assert "storage unavailable" in runtime_status(s)
    s.shutdown()


def test_shutdown_is_idempotent_and_blocks_reuse(rig):
    add_rule(rig)
    engine = rig.engine
    rig.sentinel.shutdown()
    rig.sentinel.shutdown()
    assert not engine.running
    with pytest.raises(runtime.StoreError):
        _ = rig.sentinel.store
    assert rig.call("sentinel_rules")["ok"] is False


def test_boot_starts_only_with_enabled_rules(fake_ctx):
    rig = Rig(fake_ctx)
    rig.sentinel.boot()
    assert rig.sentinel.engine_if_built is None  # nothing to watch: nothing started
    from harness.plugin_loader import plugin_module

    rules = plugin_module("sentinel.rules")
    rig.sentinel.store.upsert_rule(rules.RuleSpec(question="q?", source=CAM).validate())
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    rig.sentinel.boot()
    assert rig.sentinel.engine.running


def test_routes_without_a_store_are_logged_and_kept_in_memory(fake_ctx, caplog):
    def broken(cfg):
        raise runtime.StoreError("disk is read-only")

    s = runtime.Sentinel(fake_ctx, store_factory=broken, threaded=False)
    with caplog.at_level("WARNING"):
        routes = s.routes
    assert routes is not None
    assert any("routes kept in memory only" in r.getMessage() for r in caplog.records)
    s.shutdown()
