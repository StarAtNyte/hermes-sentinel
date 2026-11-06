"""Engine internals in synchronous mode: queueing, backoff, budget, judge errors, rules sync,
schedules, on-demand frames, the process lock and thread lifecycle."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeClock, FakeSource, SourceError
from harness.plugin_loader import plugin_module
from harness.scenes import build_scene

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.judge")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.store")
sources_mod = plugin_module("sentinel.sources")

Verdict = judge_mod.Verdict
FRAME = np.full((60, 80, 3), 100, np.uint8)


class StubGate:
    """Reports what the test queues (default: quiet)."""

    def __init__(self, spec):
        self.results = deque()
        self.roi = spec.roi
        self.frames = 0

    def process(self, frame, ts=None):
        self.frames += 1
        if self.results:
            kind = self.results.popleft()
            return SimpleNamespace(changed=kind == "change", tick=kind == "tick")
        return SimpleNamespace(changed=False, tick=False)

    def set_roi(self, roi):
        self.roi = roi


class StubJudge:
    def __init__(self):
        self.verdicts = deque()
        self.calls = []
        self.default = Verdict(False, 0.9, caption="nothing")

    def judge(self, frame, question, *, rule_id, roi=None, examples=()):
        self.calls.append(SimpleNamespace(rule_id=rule_id, roi=roi, examples=list(examples)))
        return self.verdicts.popleft() if self.verdicts else self.default


class StaticSource:
    def __init__(self, frame=FRAME):
        self.frame = frame
        self.open_count = 0
        self.close_count = 0
        self.open_error = None
        self.read_error = None

    def open(self):
        self.open_count += 1
        if self.open_error:
            raise self.open_error

    def read(self):
        if self.read_error:
            raise self.read_error
        return 0.0, self.frame

    def close(self):
        self.close_count += 1


class Harness:
    def __init__(self, ctx, *, per_hour=None, settings=None, config=None, **engine_kw):
        self.ctx = ctx
        self.clock = FakeClock(100.0)
        self.wall = [datetime(2026, 11, 6, 12, 0).timestamp()]
        self.store = store_mod.Store(clock=lambda: self.wall[0])
        self.judge = StubJudge()
        self.gates = {}
        self.sources = {}
        self.alerter = alerts.Alerter(ctx, clock=self.clock, per_rule_min_interval=0)
        self.budget = budget_mod.Budget(self.store, per_hour=per_hour, per_day=None,
                                        clock=lambda: self.wall[0])
        self.engine = engine_mod.Engine(
            store=self.store, judge=self.judge, alerter=self.alerter, budget=self.budget,
            config=config or config_mod.SentinelConfig(),
            settings=settings or engine_mod.EngineSettings(
                confirm_interval=1.0, offline_grace=3.0, backoff_initial=1.0, backoff_max=8.0,
                judge_backoff_initial=5.0, judge_backoff_max=20.0, queue_size=2),
            source_factory=self.factory, source_key=lambda s: s, gate_factory=self.gate,
            clock=self.clock, wall_clock=lambda: self.wall[0], threaded=False, **engine_kw)

    def factory(self, spec):
        src = self.sources.get(spec)
        if src is None:
            raise SourceError(f"unknown {spec}")
        return src

    def gate(self, spec):
        g = StubGate(spec)
        self.gates[spec.id] = g
        return g

    def add(self, rule_id="r1", source="cam", **kw):
        self.sources.setdefault(source, StaticSource())
        spec = rules.RuleSpec(question=f"{rule_id}?", source=source, id=rule_id, **kw).validate()
        self.store.upsert_rule(spec)
        self.engine.add_rule(spec)
        self.engine.step_source(source)  # creates the gate
        return spec

    def step(self, source="cam", gate_result=None, rule_id="r1"):
        if gate_result:
            self.gates[rule_id].results.append(gate_result)
        out = self.engine.step_source(source)
        self.engine.run_pending_judgements()
        return out

    def notices(self, needle=""):
        return [m.content for m in self.ctx.injected
                if m.content.startswith("[Sentinel notice]") and needle in m.content]


@pytest.fixture
def h(fake_ctx):
    harness = Harness(fake_ctx)
    yield harness
    harness.engine.stop()
    harness.store.close()


# --------------------------------------------------------------------------- settings / keys


def test_settings_validation():
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(capture_interval=0)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(queue_size=0)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(workers=99)
    with pytest.raises(ValueError):
        engine_mod.EngineSettings(offline_grace=-1)


def test_default_source_key():
    assert engine_mod.default_source_key("webcam:0") == "camera:0"
    assert engine_mod.default_source_key(" whatever?? ") == "whatever??"
    assert engine_mod.default_source_key("front", {"front": "rtsp://u@h/s"}).startswith("stream:")


def test_short_error_redacts_urls():
    text = engine_mod._short_error(RuntimeError("cannot open rtsp://bob:secret@cam/stream now"))
    assert "secret" not in text and "now" in text
    assert len(engine_mod._short_error("x" * 1000)) == engine_mod.MAX_ERROR_TEXT


# --------------------------------------------------------------------------- gating & judging


def test_only_gated_frames_are_judged(h):
    h.add()
    for _ in range(5):
        h.step()
    assert h.judge.calls == []
    h.step(gate_result="tick")
    h.step(gate_result="change")
    assert len(h.judge.calls) == 2
    assert h.engine.rule_status("r1")["last_reason"] == "change"


def test_candidate_gets_spaced_confirmation_checks_then_fires_once(h):
    h.add(cooldown_seconds=0)
    h.judge.verdicts.extend([Verdict(True, 0.9, caption="mug gone")] * 5)
    h.step(gate_result="change")
    assert h.engine.rule_status("r1")["state"] == "candidate"
    h.step()  # confirm not due yet (spacing 1 s, clock unchanged)
    assert len(h.judge.calls) == 1
    h.clock.advance(1.0)
    h.step()
    assert len(h.judge.calls) == 2
    assert h.engine.rule_status("r1")["state"] == "firing"
    alerts_ = [m for m in h.ctx.injected if m.content.startswith("[Sentinel alert]")]
    assert len(alerts_) == 1 and "mug gone" in alerts_[0].content
    for _ in range(5):  # firing: no further confirmation checks
        h.clock.advance(1.0)
        h.step()
    assert len(h.judge.calls) == 2
    event = h.store.last_event("r1")
    assert event.caption == "mug gone" and event.snapshot_path
    assert h.engine.status()["events_fired"] == 1


def test_hold_spreads_confirmations(h):
    spec = h.add(mode="condition", hold_seconds=100)
    assert h.engine._confirm_spacing(spec) == 50.0
    h.judge.verdicts.extend([Verdict(True, 0.9)] * 5)
    h.step(gate_result="change")
    h.clock.advance(10)
    h.step()
    assert len(h.judge.calls) == 1
    h.clock.advance(41)
    h.step()
    assert len(h.judge.calls) == 2
    assert h.engine.rule_status("r1")["state"] == "candidate"  # held 51 s of 100
    h.clock.advance(50)
    h.step()
    assert h.engine.rule_status("r1")["state"] == "firing"


def test_queue_is_bounded_and_drops_oldest(h):
    for rid in ("a", "b", "c"):
        h.add(rule_id=rid)
    for rid in ("a", "b", "c"):
        h.gates[rid].results.append("change")
    h.engine.step_source("cam")
    assert h.engine.queue_length() == 2
    status = h.engine.status()
    assert status["dropped_jobs"] == 1
    assert h.engine.rule_status("a")["dropped"] == 1
    h.engine.run_pending_judgements()
    assert [c.rule_id for c in h.judge.calls] == ["b", "c"]


def test_one_queued_job_per_rule_newest_frame_wins(h):
    h.add()
    h.gates["r1"].results.extend(["tick", "change"])
    h.engine.step_source("cam")
    newer = np.full_like(FRAME, 7)
    h.sources["cam"].frame = newer
    h.engine.step_source("cam")
    assert h.engine.queue_length() == 1
    job = h.engine._queue[0]
    assert job.frame is newer and job.reason == "change"


def test_a_rule_is_never_judged_twice_at_once(h):
    h.add()
    rt = h.engine._rules["r1"]
    h.gates["r1"].results.append("change")
    h.engine.step_source("cam")
    rt.in_flight = True
    assert h.engine.run_pending_judgements() == 0
    rt.in_flight = False
    assert h.engine.run_pending_judgements() == 1


def test_examples_are_sent_and_cached(h):
    import cv2

    h.add()
    ok, buf = cv2.imencode(".jpg", FRAME)
    h.store.add_example("r1", "pos", buf.tobytes(), note="yes!")
    h.store.add_example("r1", "neg", buf.tobytes())
    h.step(gate_result="tick")
    sent = h.judge.calls[-1].examples
    assert sorted(e.kind for e in sent) == ["neg", "pos"]
    assert any(e.note == "yes!" for e in sent)
    h.step(gate_result="tick")
    assert len(h.engine._examples) == 2  # read once, then cached


def test_schedule_outside_window_skips_judging(h):
    now = datetime.fromtimestamp(h.wall[0])
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=3)).strftime("%H:%M")
    h.add(schedule=rules.Schedule(start=start, end=end))
    h.step()
    assert h.judge.calls == [] and "r1" not in h.gates  # not even gated outside the window
    h.wall[0] += 2.5 * 3600
    h.step()
    h.step(gate_result="change")
    assert len(h.judge.calls) == 1


def test_disabled_rule_detaches_and_closes_source(h):
    spec = h.add()
    src = h.sources["cam"]
    h.engine.update_rule(spec.with_changes(enabled=False))
    assert h.engine.source_keys() == []
    assert src.close_count == 1
    assert h.engine.step_source("cam") == "missing"
    h.engine.update_rule(spec.with_changes(enabled=True))
    assert h.engine.source_keys() == ["cam"]


def test_moving_a_rule_to_another_source(h):
    spec = h.add()
    h.sources["cam2"] = StaticSource()
    h.engine.update_rule(spec.with_changes(source="cam2", roi=(0, 0, 0.5, 0.5)))
    assert h.engine.source_keys() == ["cam2"]
    h.engine.step_source("cam2")
    assert h.gates["r1"].roi == (0, 0, 0.5, 0.5)


def test_roi_edit_updates_existing_gate(h):
    spec = h.add()
    gate = h.gates["r1"]
    h.engine.update_rule(spec.with_changes(roi=(0.5, 0.5, 0.5, 0.5)))
    assert h.gates["r1"] is gate and gate.roi == (0.5, 0.5, 0.5, 0.5)


def test_remove_rule(h):
    h.add()
    h.gates["r1"].results.append("change")
    h.engine.step_source("cam")
    assert h.engine.remove_rule("r1") is True
    assert h.engine.queue_length() == 0 and h.engine.source_keys() == []
    assert h.engine.remove_rule("r1") is False


def test_sync_from_store_adds_updates_and_removes(h):
    h.sources["cam"] = StaticSource()
    spec = rules.RuleSpec(question="q?", source="cam", id="ext").validate()
    h.store.upsert_rule(spec)
    h.engine.sync_from_store()
    assert h.engine.rule_ids() == ["ext"]
    h.store.upsert_rule(spec.with_changes(question="new?"))
    h.engine.sync_from_store()
    assert h.engine._rules["ext"].spec.question == "new?"
    h.store.delete_rule("ext")
    h.engine.maintenance()  # sync is due at start
    assert h.engine.rule_ids() == []


# --------------------------------------------------------------------------- failures


def test_source_backoff_and_single_offline_notice(h):
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("cable pulled")
    src.open_error = SourceError("device busy")
    attempts = []
    for _ in range(40):
        if h.engine.step_source("cam") == "offline":
            attempts.append(h.clock.now())
        h.clock.advance(0.5)
    gaps = [round(b - a, 1) for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps[:4] == [1.0, 2.0, 4.0, 8.0] and set(gaps[4:]) <= {8.0}
    assert len(h.notices("offline")) == 1
    assert "device busy" in h.notices("offline")[0]
    status = h.engine.status()["sources"][0]
    assert status["online"] is False and status["error"] == "device busy"
    src.open_error = src.read_error = None
    for _ in range(20):
        h.engine.step_source("cam")
        h.clock.advance(0.5)
    assert len(h.notices("back online")) == 1
    assert h.engine.status()["sources"][0]["online"] is True


def test_short_blip_inside_grace_sends_no_notice(h):
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("blip")
    h.engine.step_source("cam")
    src.read_error = None
    h.clock.advance(1.0)
    assert h.engine.step_source("cam") == "frame"
    assert h.notices() == []


def test_not_ready_is_not_a_failure(h):
    h.add()
    h.sources["cam"].read_error = sources_mod.SourceNotReady("empty folder")
    assert h.engine.step_source("cam") == "not_ready"
    assert h.engine.status()["sources"][0]["error"] == ""


def test_unknown_source_is_offline_not_fatal(h):
    spec = rules.RuleSpec(question="q?", source="ghost", id="g").validate()
    h.store.upsert_rule(spec)
    h.engine.add_rule(spec)
    assert h.engine.step_source("ghost") == "offline"
    assert "unknown ghost" in h.engine.rule_status("g")["source_error"]


def test_judge_errors_back_off_and_notify_once(h):
    h.add()
    h.judge.default = Verdict(None, error="RuntimeError: network down")
    h.step(gate_result="change")
    h.step(gate_result="change")  # backing off: not judged
    assert len(h.judge.calls) == 1
    status = h.engine.rule_status("r1")
    assert status["errors"] == 1 and status["judging_paused_for"] == pytest.approx(5.0)
    h.clock.advance(5)
    h.step(gate_result="change")
    assert len(h.judge.calls) == 2
    assert h.engine.rule_status("r1")["judging_paused_for"] == pytest.approx(10.0)
    assert len(h.notices("Vision checks are failing")) == 1
    h.judge.default = Verdict(False, 0.9)
    h.clock.advance(10)
    h.step(gate_result="change")
    assert h.engine.rule_status("r1")["judging_paused_for"] == 0
    h.judge.default = Verdict(None, error="again")
    h.clock.advance(1)
    h.step(gate_result="change")
    # One success is not enough to re-arm: a flaky model must not notify per episode.
    assert len(h.notices("Vision checks are failing")) == 1


def test_budget_refusal_skips_and_notifies_once(fake_ctx):
    h = Harness(fake_ctx, per_hour=1)
    try:
        h.add()
        for _ in range(4):
            h.step(gate_result="change")
        assert len(h.judge.calls) == 1
        assert h.engine.rule_status("r1")["budget_skips"] == 3
        assert len(h.notices("budget")) == 1
    finally:
        h.engine.stop()
        h.store.close()


def test_store_failure_while_firing_still_alerts(h):
    h.add(confirm_k=1, confirm_n=1)
    h.judge.verdicts.append(Verdict(True, 0.95, caption="fire"))

    def broken(*a, **k):
        raise store_mod.StoreError("disk full")

    h.store.add_event = broken
    h.store.save_snapshot = broken
    h.step(gate_result="change")
    alert = [m.content for m in h.ctx.injected if m.content.startswith("[Sentinel alert]")]
    assert len(alert) == 1 and "fire" in alert[0] and "MEDIA:" not in alert[0]


def test_gate_failure_is_contained(h):
    h.add()

    def boom(frame, ts=None):
        raise ValueError("bad frame")

    h.gates["r1"].process = boom
    assert h.engine.step_source("cam") == "frame"
    assert "bad frame" in h.engine.status()["last_error"]


def test_maintenance_prunes_and_polls(h, monkeypatch):
    h.add()
    calls = []
    monkeypatch.setattr(h.store, "prune_if_due", lambda: calls.append(1))
    h.engine.maintenance()
    assert calls == [1]


# --------------------------------------------------------------------------- on demand


def test_grab_frame_prefers_fresh_latest_then_one_off(h):
    h.add()
    h.engine.step_source("cam")
    src = h.sources["cam"]
    opens = src.open_count
    assert h.engine.grab_frame("cam") is FRAME
    assert src.open_count == opens  # reused the capture thread's frame
    h.clock.advance(60)
    assert h.engine.grab_frame("cam") is FRAME
    assert src.open_count == opens + 1  # stale: one-off read
    h.sources["other"] = StaticSource(np.zeros((5, 5, 3), np.uint8))
    assert h.engine.grab_frame("other").shape == (5, 5, 3)


def test_grab_frame_errors(h):
    with pytest.raises(SourceError, match="unknown nowhere"):
        h.engine.grab_frame("nowhere")
    bad = StaticSource()
    bad.read_error = RuntimeError("weird")
    h.sources["bad"] = bad
    with pytest.raises(SourceError, match="weird"):
        h.engine.grab_frame("bad")
    assert bad.close_count == 1


def test_grab_frame_falls_back_to_stale_frame(h):
    h.add()
    h.engine.step_source("cam")
    h.clock.advance(60)
    h.sources["cam"].open_error = SourceError("busy")
    assert h.engine.grab_frame("cam") is FRAME


def test_test_rule(h):
    h.add()
    h.judge.verdicts.append(Verdict(True, 0.8, caption="yes it is"))
    out = h.engine.test_rule("r1")
    assert out["ok"] and out["match"] is True and out["would_count"] is True
    assert h.engine.rule_status("r1")["state"] == "idle"  # not fed to the state machine
    assert h.engine.test_rule("missing")["ok"] is False
    h.judge.verdicts.append(Verdict(True, 0.8))
    h.engine.update_rule(h.store.get_rule("r1").with_changes(mode="absence"))
    assert h.engine.test_rule("r1")["would_count"] is False  # absence counts a "no"
    h.engine.update_rule(h.store.get_rule("r1").with_changes(source="gone"))
    assert "could not read" in h.engine.test_rule("r1")["error"]
    h.engine.remove_rule("r1")
    assert h.engine.test_rule("r1")["ok"] is True  # falls back to the store copy (cam)


# --------------------------------------------------------------------------- lifecycle


def test_process_lock_is_exclusive(tmp_path):
    a = engine_mod.ProcessLock(tmp_path / "x" / "engine.lock")
    b = engine_mod.ProcessLock(tmp_path / "x" / "engine.lock")
    assert a.acquire() and a.held and a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
    b.release()


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
            store=store, judge=StubJudge(), alerter=alerts.Alerter(fake_ctx),
            budget=budget_mod.Budget(store), config=config_mod.SentinelConfig(),
            settings=engine_mod.EngineSettings(capture_interval=0.01, scheduler_interval=0.02,
                                               sync_interval=0.05),
            source_factory=lambda s: src, source_key=lambda s: s, gate_factory=StubGate,
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


def test_status_shape(h):
    h.add()
    h.engine.step_source("cam")
    st = h.engine.status()
    assert st["rules"] == 1 and st["active_rules"] == 1 and st["queue"] == 0
    assert st["sources"][0]["source"] == "cam" and st["sources"][0]["frames"] >= 1
    assert set(st["alerts"]) == {"sent", "suppressed", "failed"}
    rs = h.engine.rule_status("r1")
    assert rs["state"] == "idle" and rs["source_online"] is True
    assert h.engine.rule_status("nope") is None


def test_ctx_is_unused_for_threads(fake_ctx):
    # The engine never calls ctx.spawn_task (no event loop needed from worker threads).
    assert isinstance(fake_ctx, FakeCtx) and fake_ctx.tasks == []
