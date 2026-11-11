"""Judge runner outcomes: examples, judge errors, budget refusal, firing."""

from __future__ import annotations

import pytest
from harness.engine_rig import FRAME, Harness, Verdict
from harness.plugin_loader import plugin_module

store_mod = plugin_module("sentinel.storage")


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
    assert len(h.engine.examples.entries) == 2  # read once, then cached


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


def test_fired_event_records_how_its_alert_was_delivered(h):
    h.add(confirm_k=1, confirm_n=1)
    h.judge.verdicts.append(Verdict(True, 0.95, caption="fire"))
    h.step(gate_result="change")
    event = h.store.last_event("r1")
    assert event.delivery == "delivered" and event.delivery_ts is not None


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
