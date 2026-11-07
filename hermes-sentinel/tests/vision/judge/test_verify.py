"""Second-opinion call and how two verdicts combine."""

from __future__ import annotations

import pytest
from harness.judge_helpers import TASK, frame, llm_with
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
Judge = judge_mod.Judge
Verdict = judge_mod.Verdict
combine = judge_mod.combine_verdicts


def test_verify_sends_one_closeup_with_its_own_wording():
    llm = llm_with({"match": True, "confidence": 0.9, "reason": "mug handle", "caption": "mug"})
    verdict = Judge(llm).verify(frame(), "Is the mug gone?", rule_id="r1")
    assert verdict.ok and verdict.match is True
    call = llm.calls[0]
    assert call.task == TASK and call.purpose == "sentinel:verify:r1"
    assert len(call.images) == 1
    assert "strict, independent reviewer" in call.kwargs["instructions"]


def test_verify_failures_become_error_verdicts():
    assert not Judge(llm_with(TimeoutError("x"))).verify(frame(), "q?", rule_id="r").ok
    assert not Judge(llm_with()).verify("bad", "q?", rule_id="r").ok


def test_agreement_averages_the_confidence():
    first = Verdict(True, 0.7, reason="a", caption="cap", elapsed=1.0, model="m")
    second = Verdict(True, 0.9, reason="b", caption="other", elapsed=2.0)
    merged = combine(first, second)
    assert merged.match is True and merged.confidence == pytest.approx(0.8)
    assert merged.reason == "a" and merged.caption == "cap" and merged.elapsed == 3.0


def test_disagreement_lets_the_closer_look_win():
    first = Verdict(True, 0.75, caption="a person", model="m")
    second = Verdict(False, 0.85, reason="just a coat", caption="")
    merged = combine(first, second)
    assert merged.match is False and merged.confidence == pytest.approx(0.85)
    assert merged.reason.startswith("Second opinion disagreed") and merged.caption == "a person"


def test_an_unusable_second_opinion_changes_nothing():
    first = Verdict(True, 0.75)
    assert combine(first, Verdict(None, error="down")) is first
