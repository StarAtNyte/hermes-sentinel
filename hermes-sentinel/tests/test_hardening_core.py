"""Review hardening of the core modules: shutdown races, locks, cost guards, budget text,
config floors, delayed boot and context propagation."""

from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.judge")
rules = plugin_module("sentinel.rules")
runtime = plugin_module("sentinel.runtime")
sources_mod = plugin_module("sentinel.sources")
store_mod = plugin_module("sentinel.store")

Verdict = judge_mod.Verdict
FRAME = np.full((60, 80, 3), 100, np.uint8)
T0 = 1_762_430_400.0  # top of an hour


class StubGate:
    def __init__(self, spec):
        self.results = deque()

    def process(self, frame, ts=None):
        kind = self.results.popleft() if self.results else ""
        return SimpleNamespace(changed=kind == "change", tick=kind == "tick")

    def set_roi(self, roi):
        pass


class StubJudge:
    def __init__(self, default=None):
        self.calls = 0
        self.default = default or Verdict(False, 0.9, caption="nothing")
        self.on_call = None

    def judge(self, frame, question, *, rule_id, roi=None, examples=()):
        self.calls += 1
        if self.on_call:
            self.on_call()
        return self.default


class Source:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.read_gate = None
        self.read_started = threading.Event()
        self.seen = None

    def open(self):
        self.opens += 1
        self.seen = ctxvar.get()

    def read(self):
        self.read_started.set()
        if self.read_gate is not None:
            self.read_gate.wait(10)
        return 0.0, FRAME

    def close(self):
        self.closes += 1


ctxvar: contextvars.ContextVar[str] = contextvars.ContextVar("hardening_var", default="none")


class H:
    def __init__(self, ctx, *, settings=None, lock_path=None, threaded=False, judge=None):
        self.ctx = ctx
        self.clock = FakeClock(100.0)
        self.wall = [datetime(2026, 11, 6, 12, 0).timestamp()]
        self.store = store_mod.Store(clock=lambda: self.wall[0])
        self.judge = judge or StubJudge()
        self.source = Source()
        self.gates = {}
        self.engine = engine_mod.Engine(
            store=self.store, judge=self.judge,
            alerter=alerts.Alerter(ctx, clock=self.clock, per_rule_min_interval=0),
            budget=budget_mod.Budget(self.store, per_hour=None, per_day=None,
                                     clock=lambda: self.wall[0]),
            config=config_mod.SentinelConfig(),
            settings=settings or engine_mod.EngineSettings(
                confirm_interval=1.0, judge_backoff_initial=1.0, judge_backoff_max=2.0,
                judge_error_rearm=30.0, sync_interval=0.05, scheduler_interval=0.05,
                capture_interval=0.01),
            source_factory=lambda s: self.source, source_key=lambda s: s,
            gate_factory=self.gate, clock=self.clock, wall_clock=lambda: self.wall[0],
            lock_path=lock_path, threaded=threaded)

    def gate(self, spec):
        g = StubGate(spec)
        self.gates[spec.id] = g
        return g

    def add(self, rule_id="r1", **kw):
        spec = rules.RuleSpec(question="q?", source="cam", id=rule_id, **kw).validate()
        self.store.upsert_rule(spec)
        self.engine.add_rule(spec)
        self.engine.step_source("cam")
        return spec

    def step(self, result=None, rule_id="r1"):
        if result:
            self.gates[rule_id].results.append(result)
        out = self.engine.step_source("cam")
        self.engine.run_pending_judgements()
        return out

    def injected(self, prefix):
        return [m.content for m in self.ctx.injected if m.content.startswith(prefix)]

    def close(self):
        self.engine.stop()
        self.store.close()


@pytest.fixture
def h(fake_ctx):
    harness = H(fake_ctx)
    yield harness
    harness.close()


# ---------------------------------------------------------------- 3: shutdown is final


def test_engine_never_restarts_after_stop(h):
    h.add()
    assert h.engine.running
    h.engine.stop()
    assert h.engine.start() is False and not h.engine.running
    h.engine.add_rule(rules.RuleSpec(question="q?", source="cam", id="r2").validate())
    assert not h.engine.running


def test_standby_does_not_restart_after_stop(fake_ctx, tmp_path):
    lock = tmp_path / "engine.lock"
    owner = engine_mod.ProcessLock(lock)
    assert owner.acquire()
    h = H(fake_ctx, lock_path=lock, threaded=True)
    try:
        assert h.engine.start() is False
        h.engine.stop()
        owner.release()
        time.sleep(0.2)
        assert not h.engine.running
        assert h.engine.start() is False
        assert not [t for t in threading.enumerate() if t.name.startswith("sentinel-")]
    finally:
        h.store.close()


# ---------------------------------------------------------------- 15: takeover, one standby


def test_standby_takeover_spawns_no_second_standby(fake_ctx, tmp_path):
    lock = tmp_path / "engine.lock"
    owner = engine_mod.ProcessLock(lock)
    assert owner.acquire()
    h = H(fake_ctx, lock_path=lock, threaded=True)
    try:
        assert h.engine.start() is False
        assert h.engine.start() is False  # repeated start: still a single standby thread
        names = [t.name for t in threading.enumerate()]
        assert names.count("sentinel-standby") == 1
        # Another process grabs the lock between the probe and the takeover.
        assert h.engine._start(allow_standby=False) is False
        assert [t.name for t in threading.enumerate()].count("sentinel-standby") == 1
        owner.release()
        deadline = time.monotonic() + 5
        while not h.engine.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.engine.running
        deadline = time.monotonic() + 5
        while any(t.name == "sentinel-standby" for t in threading.enumerate()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        h.close()


# ---------------------------------------------------------------- 4: no lock across I/O


def test_worker_lock_is_free_while_read_blocks(h):
    h.add()
    worker = h.engine._workers["cam"]
    h.source.read_gate = threading.Event()
    t = threading.Thread(target=h.engine.step_source, args=("cam",), daemon=True)
    t.start()
    assert h.source.read_started.wait(5)
    try:
        assert worker.lock.acquire(timeout=1.0)  # not held across read()
        worker.lock.release()
        frame = h.engine.grab_frame("cam")  # served from the latest frame, not blocked
        assert frame is not None
    finally:
        h.source.read_gate.set()
        t.join(5)


def test_stop_does_not_block_on_a_stuck_capture_thread(fake_ctx):
    h = H(fake_ctx, threaded=True, settings=engine_mod.EngineSettings(
        capture_interval=0.01, stop_timeout=0.3, scheduler_interval=0.05, sync_interval=5))
    try:
        h.source.read_gate = threading.Event()
        h.add()
        assert h.source.read_started.wait(5)
        started = time.monotonic()
        h.engine.stop()
        assert time.monotonic() - started < 2.0
        deadline = time.monotonic() + 5
        while h.source.closes < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.source.closes >= 1  # closed by a helper thread, not by the stuck one
    finally:
        h.source.read_gate.set()
        h.store.close()


# ---------------------------------------------------------------- 5: cost guards


def test_no_judging_during_cooldown(h):
    h.add(confirm_k=1, confirm_n=1, cooldown_seconds=300)
    h.judge.default = Verdict(True, 0.95, caption="hit")
    h.step("change")
    assert h.judge.calls == 1 and len(h.injected("[Sentinel alert]")) == 1
    h.judge.default = Verdict(False, 0.95)  # the scene clears: the rule re-arms into cooldown
    for _ in range(4):
        h.clock.advance(1.0)
        h.step("change")
    assert h.engine.rule_status("r1")["state"] == "cooldown"
    calls = h.judge.calls
    for _ in range(5):
        h.clock.advance(1.0)
        h.step("change")
        h.step("tick")
    assert h.judge.calls == calls  # nothing queued while cooling down
    h.clock.advance(300)
    h.step("change")
    assert h.judge.calls == calls + 1


def test_default_gate_stretches_idle_tick_for_trigger_rules(h):
    h.engine.config = config_mod.SentinelConfig(idle_check_seconds=300.0)
    trig = rules.RuleSpec(question="q?", source="cam", id="t").validate()
    cond = rules.RuleSpec(question="q?", source="cam", id="c", mode="condition",
                          hold_seconds=10).validate()
    assert h.engine._default_gate(trig)._config.idle_check_seconds == 6 * 3600.0
    assert h.engine._default_gate(cond)._config.idle_check_seconds == 300.0
    h.engine.config = config_mod.SentinelConfig(idle_check_seconds=0.0)
    assert h.engine._default_gate(trig)._config.idle_check_seconds == 0.0


def test_judge_error_notice_rearms_only_after_sustained_success(h):
    h.add()
    h.judge.default = Verdict(None, error="boom")
    h.step("change")
    assert len(h.injected("[Sentinel notice]")) == 1
    h.judge.default = Verdict(False, 0.9)
    h.clock.advance(10)
    h.step("change")  # first success starts the streak
    h.judge.default = Verdict(None, error="boom")
    h.clock.advance(1)
    h.step("change")
    h.judge.default = Verdict(False, 0.9)
    h.clock.advance(10)
    h.step("change")
    h.judge.default = Verdict(None, error="boom")
    h.clock.advance(1)
    h.step("change")
    assert len(h.injected("[Sentinel notice]")) == 1  # flapping: still one notice
    h.judge.default = Verdict(False, 0.9)
    for _ in range(3):
        h.clock.advance(15)
        h.step("change")
    assert h.engine._judge_errors == {}  # 30 s of continuous success re-armed it
    h.judge.default = Verdict(None, error="boom")
    h.clock.advance(1)
    h.step("change")
    assert len(h.injected("[Sentinel notice]")) == 2


def test_judge_error_notice_is_per_source(h):
    h.engine._judge_errors.clear()
    state = h.engine._judge_errors.setdefault("a", [True, None])
    h.engine._judge_succeeded("b", 1.0)  # unrelated source: untouched
    assert h.engine._judge_errors == {"a": state}


# ---------------------------------------------------------------- 14: late verdict after stop


def test_verdict_after_stop_is_dropped(h):
    h.add(confirm_k=1, confirm_n=1)
    h.judge.default = Verdict(True, 0.95, caption="late")
    h.judge.on_call = h.engine.stop  # unloaded while the model is thinking
    h.step("change")
    assert h.judge.calls == 1
    assert h.injected("[Sentinel alert]") == []
    assert h.store.last_event("r1") is None


# ---------------------------------------------------------------- 20: context propagation


def test_capture_thread_inherits_starting_context(fake_ctx):
    h = H(fake_ctx, threaded=True)
    try:
        ctxvar.set("tool-call-scope")
        h.add()
        deadline = time.monotonic() + 5
        while h.source.opens < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.source.seen == "tool-call-scope"
    finally:
        ctxvar.set("none")
        h.close()


def test_open_timeout_thread_inherits_context():
    token = ctxvar.set("scoped")
    try:
        out = sources_mod._call_with_timeout(lambda: ctxvar.get(), 5.0)
    finally:
        ctxvar.reset(token)
    assert out == "scoped"


# ---------------------------------------------------------------- 16: budget retry text


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


# ---------------------------------------------------------------- 11: config floors


def test_zero_retention_and_storage_fall_back_with_problems():
    cfg = config_mod.SentinelConfig.load(
        {"retention_days": 0, "max_storage_mb": 0}.get if False else
        (lambda k, d=None: {"retention_days": 0, "max_storage_mb": 0}.get(k, d))
    )
    assert cfg.retention_days == 7.0 and cfg.max_storage_mb == 500.0
    assert any("retention_days" in p for p in cfg.problems)
    assert any("max_storage_mb" in p for p in cfg.problems)


def test_store_invalid_limits_are_store_errors(plugin_data_dir):
    with pytest.raises(store_mod.StoreError):
        store_mod.Store(retention_days=0)
    with pytest.raises(store_mod.StoreError):
        store_mod.Store(max_storage_mb=-1)


# ---------------------------------------------------------------- 7 / 6 / 18: runtime


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
        while s._pruning:  # the first access ran the real (empty) prune
            time.sleep(0.01)
        store.prune_if_due = lambda *a, **k: done.set()
        store.prune_due = lambda *a, **k: True
        _ = s.store
        assert done.wait(5)
    finally:
        s.shutdown()
