"""Gate before judge: which frames become judge jobs."""

from __future__ import annotations

from datetime import datetime, timedelta

from harness.engine_rig import Verdict
from harness.plugin_loader import plugin_module

timing_mod = plugin_module("sentinel.engine.jobs.timing")
rules = plugin_module("sentinel.rules")


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
    assert timing_mod.confirm_spacing(spec, h.engine.state.settings.confirm_interval) == 50.0
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


def test_gate_failure_is_contained(h):
    h.add()

    def boom(frame, ts=None):
        raise ValueError("bad frame")

    h.gates["r1"].process = boom
    assert h.engine.step_source("cam") == "frame"
    assert "bad frame" in h.engine.status()["last_error"]
