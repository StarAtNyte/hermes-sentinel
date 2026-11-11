"""Hardening of the core modules outside the engine: budget text, config floors, delayed boot
and background prune."""

from __future__ import annotations

import threading
import time

import pytest
from harness.fake_ctx import FakeCtx
from harness.plugin_loader import plugin_module

budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")
runtime = plugin_module("sentinel.runtime")
store_mod = plugin_module("sentinel.storage")
T0 = 1_762_430_400.0  # top of an hour


@pytest.fixture
def clock():
    class C:
        t = T0

        def __call__(self):
            return self.t

    return C()


def test_day_cap_retry_uses_oldest_bucket(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    try:
        b = budget_mod.Budget(s, per_hour=None, per_day=5, clock=clock)
        assert b.try_consume(3)
        clock.t = T0 + 5 * 3600
        assert b.try_consume(2)
        clock.t = T0 + 6 * 3600 + 120
        st = b.status()
        assert st.exhausted
        # The first 3 calls leave the window 24 h after their hour began.
        assert st.retry_after_seconds == pytest.approx(18 * 3600 - 120)
        assert "resumes in about 1078 min" in st.describe()
    finally:
        s.close()


def test_day_cap_retry_waits_for_enough_buckets(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    try:
        b = budget_mod.Budget(s, per_hour=None, per_day=2, clock=clock)
        assert b.try_consume(1)
        clock.t = T0 + 3600
        assert b.try_consume(1)
        clock.t = T0 + 7200
        # used 2 of 2: dropping the oldest bucket leaves 1 < 2.
        assert b.status().retry_after_seconds == pytest.approx(22 * 3600)
    finally:
        s.close()


def test_hour_cap_retry_is_next_hour(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    try:
        b = budget_mod.Budget(s, per_hour=1, per_day=None, clock=clock)
        assert b.try_consume()
        clock.t = T0 + 600
        assert b.status().retry_after_seconds == pytest.approx(3000)
    finally:
        s.close()


def test_zero_retention_and_storage_fall_back_with_problems():
    cfg = config_mod.SentinelConfig.load(
        {"retention_days": 0, "max_storage_mb": 0}.get
        if False
        else (lambda k, d=None: {"retention_days": 0, "max_storage_mb": 0}.get(k, d))
    )
    assert cfg.retention_days == 7.0 and cfg.max_storage_mb == 500.0
    assert any("retention_days" in p for p in cfg.problems)
    assert any("max_storage_mb" in p for p in cfg.problems)


def test_store_invalid_limits_are_store_errors(plugin_data_dir):
    with pytest.raises(store_mod.StoreError):
        store_mod.Store(retention_days=0)
    with pytest.raises(store_mod.StoreError):
        store_mod.Store(max_storage_mb=-1)


def test_alert_and_notice_rates_are_lowered(fake_ctx):
    s = runtime.Sentinel(fake_ctx, threaded=False)
    try:
        assert s.alerter._alerts.cap == 20 and s.alerter._notices.cap == 6
    finally:
        s.shutdown()


def _sentinel_with_rule(fake_ctx, tmp_path):
    folder = tmp_path / "frames"
    folder.mkdir()
    s = runtime.Sentinel(fake_ctx, threaded=False, use_process_lock=False)
    s.store.upsert_rule(rules.RuleSpec(question="q?", source=f"folder:{folder}").validate())
    return s


def test_boot_delayed_starts_later_and_shutdown_cancels(fake_ctx, tmp_path):
    s = _sentinel_with_rule(fake_ctx, tmp_path)
    s.boot_delayed(0.2)
    assert s.engine_if_built is None  # nothing opened at load time
    deadline = time.monotonic() + 5
    while s.engine_if_built is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert s.engine_if_built is not None and s.engine_if_built.running
    s.shutdown()

    fake2 = FakeCtx(plugin_id=fake_ctx.plugin_id) if hasattr(fake_ctx, "plugin_id") else fake_ctx
    s2 = runtime.Sentinel(fake2, threaded=False, use_process_lock=False)
    s2.boot_delayed(0.3)
    s2.shutdown()
    time.sleep(0.5)
    assert s2.engine_if_built is None
    s2.boot_delayed(0.0)  # closed: ignored
    assert not [t for t in threading.enumerate() if t.name == "sentinel-boot"]


def test_housekeeping_prunes_without_enabled_rules(fake_ctx):
    s = runtime.Sentinel(fake_ctx, threaded=False)
    try:
        calls = []
        store = s.store
        store.prune_if_due = lambda *a, **k: calls.append(1)
        s.boot()  # no enabled rules: engine stays off but retention still runs
        assert calls and s.engine_if_built is None
    finally:
        s.shutdown()


def test_store_access_kicks_background_prune_when_engine_idle(fake_ctx):
    s = runtime.Sentinel(fake_ctx, threaded=False)
    try:
        done = threading.Event()
        store = s.store
        while s._pruner.busy:  # the first access ran the real (empty) prune
            time.sleep(0.01)
        store.prune_if_due = lambda *a, **k: done.set()
        store.prune_due = lambda *a, **k: True
        _ = s.store
        assert done.wait(5)
    finally:
        s.shutdown()
