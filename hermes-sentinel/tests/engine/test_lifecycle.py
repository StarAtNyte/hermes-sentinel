"""Standby and thread lifecycle."""

from __future__ import annotations

import threading
import time

from harness.engine_rig import Harness, StubGate, StubJudge
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeClock, FakeSource
from harness.plugin_loader import plugin_module
from harness.scenes import build_scene

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.storage")


def test_second_engine_stands_by(fake_ctx, tmp_path):
    lock = tmp_path / "engine.lock"
    owner = engine_mod.ProcessLock(lock)
    assert owner.acquire()
    h = Harness(fake_ctx, lock_path=lock)
    try:
        assert h.engine.start() is False
        assert h.engine.standing_by and not h.engine.running
        owner.release()
        assert h.engine.start() is True and h.engine.running
    finally:
        h.engine.stop()
        h.store.close()


def test_threaded_lifecycle_and_idempotent_stop(fake_ctx):
    clock = FakeClock()
    src = FakeSource(build_scene("static_scene", n_frames=10), clock, loop=True)
    store = store_mod.Store()
    try:
        eng = engine_mod.Engine(
            store=store,
            judge=StubJudge(),
            alerter=alerts.Alerter(fake_ctx),
            budget=budget_mod.Budget(store),
            config=config_mod.SentinelConfig(),
            settings=engine_mod.EngineSettings(
                capture_interval=0.01, scheduler_interval=0.02, sync_interval=0.05
            ),
            source_factory=lambda s: src,
            source_key=lambda s: s,
            gate_factory=StubGate,
        )
        spec = rules.RuleSpec(question="q?", source="cam", id="t1").validate()
        store.upsert_rule(spec)
        eng.add_rule(spec)
        assert eng.start() is True  # already running: idempotent
        deadline = time.monotonic() + 5
        while src.frames_read < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert src.frames_read >= 5
        names = {t.name for t in threading.enumerate()}
        assert {"sentinel-judge-0", "sentinel-judge-1", "sentinel-scheduler"} <= names
        assert all(t.daemon for t in threading.enumerate() if t.name.startswith("sentinel-"))
        eng.remove_rule("t1")  # the capture thread exits and closes its source
        deadline = time.monotonic() + 5
        while src.close_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert src.close_count >= 1
        eng.stop()
        eng.stop()
        assert not eng.running
        assert not [t for t in threading.enumerate() if t.name.startswith("sentinel-")]
    finally:
        store.close()


def test_ctx_is_unused_for_threads(fake_ctx):
    # The engine never calls ctx.spawn_task (no event loop needed from worker threads).
    assert isinstance(fake_ctx, FakeCtx) and fake_ctx.tasks == []
