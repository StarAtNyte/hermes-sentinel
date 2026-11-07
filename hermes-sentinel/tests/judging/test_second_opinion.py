"""Second-opinion eligibility and the crop around the changed region."""

from __future__ import annotations

import numpy as np
import pytest
from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

so = plugin_module("sentinel.judging.second_opinion")
policy_mod = plugin_module("sentinel.judging.policy")
cap_mod = plugin_module("sentinel.judging.hourly_cap")
rules = plugin_module("sentinel.rules")
vision = plugin_module("sentinel.vision.judge")

Verdict = vision.Verdict
RuleState = rules.RuleState


class RecordingJudge:
    def __init__(self, reply):
        self.reply = reply
        self.crops = []

    def verify(self, crop, question, *, rule_id):
        self.crops.append(crop)
        return self.reply


def build(reply=None, *, allow=True, **policy):
    judge = RecordingJudge(reply or Verdict(True, 0.9))
    reserved = []

    def reserve(spec):
        reserved.append(spec.id)
        return allow

    opinion = so.SecondOpinion(
        judge,
        policy_mod.JudgingPolicy(**policy),
        cap_mod.HourlyCap(6, clock=FakeClock(0.0)),
        reserve,
        lambda: False,
    )
    return opinion, judge, reserved


def spec(**kw):
    return rules.RuleSpec(question="q?", source="cam", **kw)


@pytest.mark.parametrize(
    ("confidence", "wanted"),
    [(0.5, False), (0.69, False), (0.7, True), (0.79, True), (0.8, False), (0.95, False)],
)
def test_only_hits_inside_the_band_are_wanted(confidence, wanted):
    opinion, _, _ = build()
    assert opinion.wanted(spec(), RuleState.IDLE, Verdict(True, confidence)) is wanted


def test_a_no_is_never_wanted_for_a_trigger_but_is_for_an_absence_rule():
    opinion, _, _ = build()
    assert not opinion.wanted(spec(), RuleState.IDLE, Verdict(False, 0.75))
    assert opinion.wanted(
        spec(mode="absence", hold_seconds=1), RuleState.IDLE, Verdict(False, 0.75)
    )


@pytest.mark.parametrize("state", [RuleState.FIRING, RuleState.COOLDOWN])
def test_no_check_when_the_verdict_cannot_move_the_rule(state):
    opinion, _, _ = build()
    assert not opinion.wanted(spec(), state, Verdict(True, 0.75))


def test_failed_verdicts_and_high_threshold_rules_are_skipped():
    opinion, _, _ = build()
    assert not opinion.wanted(spec(), RuleState.IDLE, Verdict(None, error="x"))
    assert not opinion.wanted(spec(confidence_threshold=0.85), RuleState.IDLE, Verdict(True, 0.85))


def test_disabled_policies_want_nothing():
    for policy in ({"second_opinion": False}, {"second_opinion_per_hour": 0}):
        opinion, _, _ = build(**policy)
        assert not opinion.wanted(spec(), RuleState.IDLE, Verdict(True, 0.75))


def test_review_reserves_budget_before_the_call_and_merges():
    opinion, judge, reserved = build(Verdict(True, 0.95))
    frame = np.zeros((100, 200, 3), np.uint8)
    out = opinion.review(
        spec(id="r1"), RuleState.IDLE, frame, (20, 20, 40, 30), Verdict(True, 0.75)
    )
    assert reserved == ["r1"] and len(judge.crops) == 1
    assert out.confidence == pytest.approx(0.85)


def test_review_does_nothing_when_not_wanted_and_spends_nothing():
    opinion, judge, reserved = build()
    verdict = Verdict(True, 0.95)
    assert opinion.review(spec(), RuleState.IDLE, None, None, verdict) is verdict
    assert reserved == [] and judge.crops == []


def test_refused_budget_keeps_the_verdict_and_frees_the_slot():
    opinion, judge, reserved = build(allow=False)
    verdict = Verdict(True, 0.75)
    frame = np.zeros((50, 50, 3), np.uint8)
    assert opinion.review(spec(), RuleState.IDLE, frame, None, verdict) is verdict
    assert judge.crops == [] and len(reserved) == 1
    assert opinion._cap.used() == 0


def test_crop_is_padded_clamped_and_never_tiny():
    frame = np.arange(200 * 300).reshape(200, 300).astype(np.uint8)
    crop = so.padded_crop(frame, (100, 80, 40, 20))
    assert crop.shape[0] >= so.MIN_CROP_SIDE and crop.shape[1] >= 40 + 2 * 10
    corner = so.padded_crop(frame, (0, 0, 10, 10))
    assert corner.shape[0] <= 200 and corner.shape[1] <= 300 and corner.shape[0] > 10
    edge = so.padded_crop(frame, (290, 190, 10, 10))
    assert edge.shape[0] <= 200 and edge.shape[1] <= 300


def test_no_box_means_the_whole_frame():
    frame = np.zeros((20, 20, 3), np.uint8)
    assert so.padded_crop(frame, None) is frame
