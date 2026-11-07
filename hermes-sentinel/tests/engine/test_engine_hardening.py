"""Engine hardening: shutdown is final, standby takeover, no lock across I/O, cost guards,
late verdicts and context propagation."""

from __future__ import annotations

import contextvars
import dataclasses
import threading
import time
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.storage")
Verdict = judge_mod.Verdict
FRAME = np.full((60, 80, 3), 100, np.uint8)


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
            store=self.store,
            judge=self.judge,
            alerter=alerts.Alerter(ctx, clock=self.clock, per_rule_min_interval=0),
            budget=budget_mod.Budget(
                self.store, per_hour=None, per_day=None, clock=lambda: self.wall[0]
            ),
            config=config_mod.SentinelConfig(),
            settings=settings
            or engine_mod.EngineSettings(
                confirm_interval=1.0,
                judge_backoff_initial=1.0,
                judge_backoff_max=2.0,
                judge_error_rearm=30.0,
                sync_interval=0.05,
                scheduler_interval=0.05,
                capture_interval=0.01,
            ),
            source_factory=lambda s: self.source,
            source_key=lambda s: s,
            gate_factory=self.gate,
            clock=self.clock,
            wall_clock=lambda: self.wall[0],
            lock_path=lock_path,
            threaded=threaded,
        )

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
        assert h.engine.lifecycle.start(allow_standby=False) is False
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


def test_worker_lock_is_free_while_read_blocks(h):
    h.add()
    worker = h.engine.state.workers["cam"]
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
    h = H(
        fake_ctx,
        threaded=True,
        settings=engine_mod.EngineSettings(
            capture_interval=0.01, stop_timeout=0.3, scheduler_interval=0.05, sync_interval=5
        ),
    )
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
    h.engine.state.config = config_mod.SentinelConfig(idle_check_seconds=300.0)
    trig = rules.RuleSpec(question="q?", source="cam", id="t").validate()
    cond = rules.RuleSpec(
        question="q?", source="cam", id="c", mode="condition", hold_seconds=10
    ).validate()
    assert h.engine.default_gate(trig)._config.idle_check_seconds == 6 * 3600.0
    assert h.engine.default_gate(cond)._config.idle_check_seconds == 300.0
    h.engine.state.config = config_mod.SentinelConfig(idle_check_seconds=0.0)
    assert h.engine.default_gate(trig)._config.idle_check_seconds == 0.0


def test_only_motion_rules_get_a_recent_frames_buffer(h):
    still = rules.RuleSpec(question="q?", source="cam", id="s").validate()
    moving = rules.RuleSpec(question="q?", source="cam", id="m", motion=True).validate()
    assert h.engine.default_gate(still)._config.recent_frames == 0
    assert h.engine.default_gate(moving)._config.recent_frames == 3
    gate = h.engine.default_gate(still)
    gate.process(np.zeros((32, 32, 3), np.uint8), 1.0)
    gate.process(np.ones((32, 32, 3), np.uint8), 3.0)
    assert gate.recent_frames() == []


def test_editing_motion_rebuilds_the_gate(h):
    spec = h.add()
    h.step("quiet")
    rt = h.engine.state.rules[spec.id]
    assert rt.gate is not None
    h.engine.update_rule(dataclasses.replace(spec, motion=True).validate())
    assert rt.gate is None


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
    assert h.engine.judge_health.errors == {}  # 30 s of continuous success re-armed it
    h.judge.default = Verdict(None, error="boom")
    h.clock.advance(1)
    h.step("change")
    assert len(h.injected("[Sentinel notice]")) == 2


def test_judge_error_notice_is_per_source(h):
    h.engine.judge_health.errors.clear()
    state = h.engine.judge_health.errors.setdefault("a", [True, None])
    h.engine.judge_health.succeeded("b", 1.0)  # unrelated source: untouched
    assert h.engine.judge_health.errors == {"a": state}


def test_verdict_after_stop_is_dropped(h):
    h.add(confirm_k=1, confirm_n=1)
    h.judge.default = Verdict(True, 0.95, caption="late")
    h.judge.on_call = h.engine.stop  # unloaded while the model is thinking
    h.step("change")
    assert h.judge.calls == 1
    assert h.injected("[Sentinel alert]") == []
    assert h.store.last_event("r1") is None


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
        timeouts = plugin_module("sentinel.sources.timeouts")
        out = timeouts.call_with_timeout(lambda: ctxvar.get(), 5.0)
    finally:
        ctxvar.reset(token)
    assert out == "scoped"
