"""Engine judging: batching, schema fallback, motion input, second opinions, budget and threads."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest
from harness.judging_rig import FRAME, JudgingRig
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")


@pytest.fixture
def make(fake_ctx):
    rigs = []

    def build(**kwargs):
        rig = JudgingRig(fake_ctx, **kwargs)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.close()


def three_rules(rig, **extra):
    for rule_id in ("a", "b", "c"):
        rig.add(rule_id, **extra)
    rig.stage("a", "b", "c")


# --------------------------------------------------------------------------- batching


def test_rules_on_one_frame_share_one_call_and_one_budget_unit(make):
    rig = make()
    three_rules(rig)
    rig.answers = {"a": (True, 0.95), "b": (False, 0.9), "c": (True, 0.92)}
    assert rig.round() == 3
    assert rig.purposes() == ["sentinel:batch"]
    assert rig.used() == 1
    statuses = {r: rig.engine.rule_status(r) for r in "abc"}
    assert all(s["checks"] == 1 for s in statuses.values())
    assert [s["state"] for s in (statuses["a"], statuses["b"], statuses["c"])] == [
        "firing",
        "idle",
        "firing",
    ]
    assert rig.store.count_events(rule_id="a") == 1 and rig.store.count_events(rule_id="c") == 1
    assert rig.store.count_events(rule_id="b") == 0
    assert len(rig.alert_texts()) == 2  # per-rule events and alerts are unchanged


def test_batch_size_is_capped(make):
    rig = make(batch_max_rules=2)
    three_rules(rig)
    rig.round()
    assert rig.purposes() == ["sentinel:batch", "sentinel:c"]
    assert rig.used() == 2


def test_batching_off_judges_each_rule_alone(make):
    rig = make(batch_judging=False)
    three_rules(rig)
    rig.round()
    assert sorted(rig.purposes()) == ["sentinel:a", "sentinel:b", "sentinel:c"]
    assert rig.used() == 3


def test_a_lone_rule_is_not_wrapped_in_a_batch(make):
    rig = make()
    rig.add("a")
    rig.stage("a")
    rig.round()
    assert rig.purposes() == ["sentinel:a"]


def test_unusable_batch_reply_falls_back_to_separate_calls_all_budgeted(make):
    rig = make()
    three_rules(rig)
    rig.batch_reply = {"verdicts": "nope"}  # fails the schema, as a host would reject
    rig.answers = {"a": (True, 0.95)}
    rig.round()
    assert rig.purposes()[0] == "sentinel:batch"
    assert sorted(rig.purposes()[1:]) == ["sentinel:a", "sentinel:b", "sentinel:c"]
    assert rig.used() == 4  # the failed batch call plus three separate calls, each counted
    assert rig.store.count_events(rule_id="a") == 1


def test_worst_case_calls_for_unusable_batch(make):
    rig = make(batch_max_rules=4)
    for rule_id in "abcd":
        rig.add(rule_id)
    rig.stage(*"abcd", bbox=BBOX)
    rig.batch_reply = "garbage"
    rig.answers = {rule_id: (True, 0.7) for rule_id in "abcd"}
    rig.verify_answer = (False, 0.9)
    rig.round()
    # one batch call plus one single per rule; rules judged in a fallback get no second opinion
    assert len(rig.purposes()) == 5
    assert not [p for p in rig.purposes() if p.startswith("sentinel:verify:")]
    assert rig.used() == 5


def test_batch_spend_is_charged_to_a_shared_id_not_the_first_rule(make):
    rig = make()
    rig.add("a")
    rig.add("b")
    rig.stage("a", "b")
    rig.round()
    assert rig.used() == 1
    assert rig.store.usage_totals(rule_id="batch") == (1, 1)
    assert rig.store.usage_totals(rule_id="a") == (0, 0)
    assert rig.store.usage_totals(rule_id="b") == (0, 0)


def test_exhausted_notice_for_a_batch_goes_to_the_first_rules_origin(make):
    rig = make(per_hour=0)
    three_rules(rig)
    rig.round()
    notices = [m for m in rig.ctx.injected if "budget" in m.content.lower()]
    assert len(notices) == 1


def test_unparseable_text_also_falls_back(make):
    rig = make()
    three_rules(rig)
    rig.batch_reply = "I cannot do that"
    rig.round()
    assert len(rig.vision_calls) == 4


def test_rules_the_batch_did_not_answer_are_judged_alone(make):
    rig = make()
    three_rules(rig)
    rig.batch_reply = {
        "verdicts": [
            {"rule_id": "a", "match": True, "confidence": 0.9},
            {"rule_id": "c", "match": False, "confidence": 0.9},
        ]
    }
    rig.round()
    assert rig.purposes() == ["sentinel:batch", "sentinel:b"]
    assert rig.used() == 2


def test_a_failed_batch_call_is_not_retried_rule_by_rule(make):
    rig = make()
    three_rules(rig)
    rig.batch_reply = TimeoutError("model unreachable")
    rig.round()
    assert rig.purposes() == ["sentinel:batch"]
    assert rig.used() == 1
    for rule_id in "abc":
        status = rig.engine.rule_status(rule_id)
        assert status["errors"] == 1 and status["checks"] == 1
    assert len(rig.ctx.injected) == 1  # one failure notice for the source, not three


def test_exhausted_budget_skips_the_whole_batch_with_one_notice(make):
    rig = make(per_hour=1)
    three_rules(rig)
    rig.round()
    assert rig.used() == 1
    rig.stage("a", "b", "c")
    rig.round()
    assert len(rig.vision_calls) == 1
    for rule_id in "abc":
        assert rig.engine.rule_status(rule_id)["budget_skips"] >= 1
    assert len([m for m in rig.ctx.injected if "budget" in m.content.lower()]) == 1


def test_fallback_calls_stop_when_the_budget_runs_out(make):
    rig = make(per_hour=2)
    three_rules(rig)
    rig.batch_reply = "garbage"
    rig.round()
    assert len(rig.vision_calls) == 2  # the batch plus one single; the rest are refused
    assert rig.used() == 2
    skipped = sum(rig.engine.rule_status(r)["budget_skips"] for r in "abc")
    assert skipped == 2


def test_rules_with_examples_or_motion_never_join_a_batch(make):
    rig = make()
    rig.add("a")
    rig.add("b")
    rig.add("m", motion=True)
    rig.stage("a", "b", "m")
    rig.round()
    assert sorted(rig.purposes()) == ["sentinel:batch", "sentinel:m"]
    assert rig.used() == 2


def test_cooldown_rules_are_not_queued_into_a_batch(make):
    rig = make()
    three_rules(rig)
    rig.answers = {"a": (True, 0.95), "b": (True, 0.95), "c": (True, 0.95)}
    rig.round()
    assert rig.used() == 1
    assert all(rig.engine.rule_status(r)["state"] == "firing" for r in "abc")
    rig.stage("a", "b", "c")
    rig.round()
    assert rig.used() == 1  # cooling down (even while latched): not queued, not judged, not paid


def test_different_frames_are_not_batched(make):
    rig = make()
    rig.add("a")
    rig.add("b")
    rig.stage("a")
    rig.round()
    rig.source.frame = np.zeros_like(FRAME)
    rig.stage("b")
    rig.round()
    assert sorted(rig.purposes()) == ["sentinel:a", "sentinel:b"]


# --------------------------------------------------------------------------- motion


def earlier_frames(count=2):
    return [
        SimpleNamespace(ts=100.0 - 1.0 * (count - i), frame=np.full((60, 80, 3), 40 * i, np.uint8))
        for i in range(count)
    ]


def test_motion_rule_sends_ordered_frames_in_a_single_call(make):
    rig = make()
    rig.add("m", motion=True)
    rig.gates["m"].recent = earlier_frames(2)
    rig.stage("m")
    rig.round()
    assert len(rig.vision_calls) == 1
    call = rig.vision_calls[0]
    assert len(call.images) == 3
    labels = [b.text for b in call.blocks if b.type == "text"]
    assert labels[1:] == [
        "Frame 1 of 3 (t = -2.0 s):",
        "Frame 2 of 3 (t = -1.0 s):",
        "Frame 3 of 3 (CURRENT, t = 0):",
    ]
    assert "time order" in call.kwargs["instructions"]
    assert rig.used() == 1


def test_motion_rule_without_history_or_with_motion_off_sends_a_still(make):
    rig = make()
    rig.add("m", motion=True)  # no recent frames yet
    rig.stage("m")
    rig.round()
    assert len(rig.vision_calls[0].images) == 1
    rig2 = make(motion_frames=0)
    rig2.add("m", motion=True)
    rig2.gates["m"].recent = earlier_frames(2)
    rig2.stage("m")
    rig2.round()
    assert len(rig2.vision_calls[0].images) == 1


def test_a_static_rule_never_gets_a_sequence(make):
    rig = make()
    rig.add("s")
    rig.gates["s"].recent = earlier_frames(2)
    rig.stage("s")
    rig.round()
    assert len(rig.vision_calls[0].images) == 1


def test_motion_frame_count_follows_the_config(make):
    rig = make(motion_frames=2)
    rig.add("m", motion=True)
    rig.gates["m"].recent = earlier_frames(3)
    rig.stage("m")
    rig.round()
    assert len(rig.vision_calls[0].images) == 2


# --------------------------------------------------------------------------- second opinion

BBOX = (100, 60, 80, 60)


def borderline(rig, confidence=0.75):
    rig.add("a")
    rig.answers = {"a": (True, confidence)}
    rig.stage("a", bbox=BBOX)


def test_borderline_hit_gets_one_budgeted_second_opinion_that_can_confirm(make):
    rig = make()
    borderline(rig, 0.75)
    rig.verify_answer = (True, 0.95)
    rig.round()
    assert rig.purposes() == ["sentinel:a", "sentinel:verify:a"]
    assert rig.used() == 2
    event = rig.store.last_event("a")
    assert event is not None and event.confidence == pytest.approx(0.85)  # average of the two


def test_a_disagreeing_second_opinion_prevents_the_alert(make):
    rig = make()
    borderline(rig, 0.75)
    rig.verify_answer = (False, 0.9)
    rig.round()
    assert rig.purposes() == ["sentinel:a", "sentinel:verify:a"]
    assert rig.store.count_events(rule_id="a") == 0 and rig.alert_texts() == []
    assert rig.engine.rule_status("a")["state"] == "idle"


def test_a_failed_second_opinion_keeps_the_first_verdict(make):
    rig = make()
    borderline(rig, 0.75)
    rig.verify_answer = TimeoutError("slow")
    rig.round()
    assert rig.store.count_events(rule_id="a") == 1
    assert rig.used() == 2


@pytest.mark.parametrize("confidence", [0.8, 0.9, 0.99])
def test_confident_verdicts_never_get_a_second_opinion(make, confidence):
    rig = make()
    borderline(rig, confidence)
    rig.round()
    assert rig.purposes() == ["sentinel:a"]


def test_verdicts_that_would_not_count_are_not_re_checked(make):
    rig = make()
    rig.add("a")
    rig.add("n")
    rig.answers = {"a": (True, 0.6), "n": (False, 0.75)}  # below threshold / a plain "no"
    rig.stage("a", "n")
    rig.round()
    assert "sentinel:verify:a" not in rig.purposes() and "sentinel:verify:n" not in rig.purposes()


def test_motion_rules_never_get_a_second_opinion(make):
    rig = make()
    rig.add("m", motion=True)
    rig.answers = {"m": (True, 0.7)}
    rig.verify_answer = (False, 0.9)
    rig.stage("m", bbox=BBOX)
    rig.round()
    assert rig.purposes() == ["sentinel:m"]
    assert rig.store.count_events(rule_id="m") == 1


def test_absence_rules_check_their_borderline_negatives(make):
    rig = make()
    rig.add("gone", mode="absence", hold_seconds=0)
    rig.answers = {"gone": (False, 0.75)}
    rig.verify_answer = (False, 0.9)
    rig.stage("gone", bbox=BBOX)
    rig.round()
    assert rig.purposes() == ["sentinel:gone", "sentinel:verify:gone"]


def test_rules_whose_threshold_is_in_the_high_band_are_never_checked(make):
    rig = make()
    rig.add("a", confidence_threshold=0.85)
    rig.answers = {"a": (True, 0.85)}
    rig.stage("a", bbox=BBOX)
    rig.round()
    assert rig.purposes() == ["sentinel:a"]


def test_hourly_cap_limits_second_opinions_and_leaves_first_verdicts_standing(make):
    rig = make(second_opinion_per_hour=1)
    for rule_id in ("a", "b"):
        rig.add(rule_id)
    rig.answers = {"a": (True, 0.75), "b": (True, 0.75)}
    rig.stage("a", "b", bbox=BBOX)
    rig.round()
    verifies = [p for p in rig.purposes() if p.startswith("sentinel:verify:")]
    assert len(verifies) == 1
    assert rig.store.count_events(rule_id="a") + rig.store.count_events(rule_id="b") == 2
    assert rig.used() == 2  # one batch call plus one second opinion


def cycle(rig, bbox=BBOX):
    """A 'no' that re-arms the rule, then a borderline 'yes'."""
    rig.answers = {"a": (False, 0.9)}
    rig.stage("a")
    rig.round()
    rig.answers = {"a": (True, 0.75)}
    rig.stage("a", bbox=bbox)
    rig.round()


def test_cap_frees_up_after_an_hour(make):
    rig = make(second_opinion_per_hour=1)
    rig.add("a", cooldown_seconds=0)

    def verifies():
        return len([p for p in rig.purposes() if p.startswith("sentinel:verify:")])

    cycle(rig)
    cycle(rig)
    assert verifies() == 1
    rig.clock._now += 3601.0
    cycle(rig)
    assert verifies() == 2


def test_second_opinion_off_or_zero_cap_means_no_extra_calls(make):
    for kwargs in ({"second_opinion": False}, {"second_opinion_per_hour": 0}):
        rig = make(**kwargs)
        borderline(rig, 0.75)
        rig.round()
        assert rig.purposes() == ["sentinel:a"]


def test_budget_refusing_the_second_opinion_keeps_the_first_verdict(make):
    rig = make(per_hour=1)
    borderline(rig, 0.75)
    rig.round()
    assert rig.purposes() == ["sentinel:a"]
    assert rig.store.count_events(rule_id="a") == 1
    assert rig.used() == 1


def test_a_refused_budget_gives_the_cap_slot_back(make):
    rig = make(per_hour=1, second_opinion_per_hour=1)
    borderline(rig, 0.75)
    rig.round()
    assert rig.engine.second_opinion._cap.used() == 0


def test_no_second_opinion_while_the_rule_is_latched_or_cooling(make):
    rig = make()
    rig.add("a", confirm_k=1, confirm_n=1, cooldown_seconds=1000)
    rig.answers = {"a": (True, 0.9)}
    rig.stage("a", bbox=BBOX)
    rig.round()  # fires: now in cooldown
    rig.answers = {"a": (True, 0.75)}
    rig.stage("a", bbox=BBOX)
    rig.round()  # latched and cooling down: not judged at all, so never re-verified
    assert rig.purposes() == ["sentinel:a"]


def test_batched_borderline_verdicts_are_each_verified(make):
    rig = make()
    for rule_id in ("a", "b"):
        rig.add(rule_id)
    rig.answers = {"a": (True, 0.75), "b": (True, 0.95)}
    rig.stage("a", "b", bbox=BBOX)
    rig.round()
    assert rig.purposes() == ["sentinel:batch", "sentinel:verify:a"]
    assert rig.used() == 2


# --------------------------------------------------------------------------- threads


def test_concurrent_workers_judge_every_rule_exactly_once(make):
    rig = make(queue_size=64)
    ids = [f"r{i}" for i in range(12)]
    for rule_id in ids:
        rig.add(rule_id)
    rig.stage(*ids)
    rig.engine.step_source("cam")
    errors: list[BaseException] = []

    def worker():
        try:
            rig.engine.run_pending_judgements()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors and not any(t.is_alive() for t in threads)
    assert all(rig.engine.rule_status(r)["checks"] == 1 for r in ids)
    assert all(not rig.engine.state.rules[r].in_flight for r in ids)
    assert rig.engine.queue_length() == 0
    # Twelve rules at four per call is three calls, however the workers interleave.
    assert rig.used() <= 12 and rig.used() >= 3
    assert len(rig.vision_calls) == rig.used()


def test_a_stopped_engine_makes_no_further_calls(make):
    rig = make()
    borderline(rig, 0.75)
    rig.engine.stop()
    assert rig.round() >= 0
    assert rig.vision_calls == []
