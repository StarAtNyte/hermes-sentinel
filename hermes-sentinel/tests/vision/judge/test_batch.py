"""Batch judging: one call, many rules; parsing and the signals that trigger fallback."""

from __future__ import annotations

import pytest
from harness.judge_helpers import TASK, frame, llm_with
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
Judge = judge_mod.Judge
BatchQuestion = judge_mod.BatchQuestion

QUESTIONS = [
    BatchQuestion("a", "Is the mug gone?"),
    BatchQuestion("b", "Is a dog on the couch?", roi=(0.1, 0.2, 0.5, 0.5)),
]


def entry(rule_id, match=True, confidence=0.9, **extra):
    return {"rule_id": rule_id, "match": match, "confidence": confidence, **extra}


def test_one_call_answers_every_rule():
    llm = llm_with(
        {"verdicts": [entry("a", caption="mug gone"), entry("b", False, 0.8, reason="no dog")]}
    )
    out = Judge(llm).judge_batch(frame(), QUESTIONS)
    assert len(llm.calls) == 1
    assert set(out.verdicts) == {"a", "b"} and out.missing == () and out.error is None
    assert out.verdicts["a"].match is True and out.verdicts["a"].caption == "mug gone"
    assert out.verdicts["b"].match is False and out.verdicts["b"].reason == "no dog"
    call = llm.calls[0]
    assert call.task == TASK and call.purpose == "sentinel:batch"
    assert len(call.images) == 1
    text = call.kwargs["instructions"]
    assert 'id "a": Is the mug gone?' in text
    assert 'id "b": Is a dog on the couch? [region: x=0.10, y=0.20' in text


def test_unknown_duplicate_and_omitted_entries():
    llm = llm_with(
        {
            "verdicts": [
                entry("a"),
                entry("a", False, 0.1),  # duplicate: the first answer wins
                entry("zzz"),  # not asked
            ]
        }
    )
    out = Judge(llm).judge_batch(frame(), QUESTIONS)
    assert set(out.verdicts) == {"a"} and out.verdicts["a"].match is True
    assert out.missing == ("b",) and not out.call_failed and out.error is None


def test_a_reply_that_breaks_the_schema_is_unusable_not_a_call_failure():
    """The host raises ValueError for it; the caller must fall back to separate calls."""
    out = Judge(llm_with({"verdicts": "nope"})).judge_batch(frame(), QUESTIONS)
    assert out.verdicts == {} and out.missing == ("a", "b")
    assert out.error and not out.call_failed


@pytest.mark.parametrize("reply", ["not json at all", {"verdicts": []}])
def test_a_reply_without_answers_is_unusable(reply):
    out = Judge(llm_with(reply)).judge_batch(frame(), QUESTIONS)
    assert out.verdicts == {} and out.missing == ("a", "b")
    assert out.error and not out.call_failed


def test_a_raised_error_is_a_call_failure():
    out = Judge(llm_with(TimeoutError("slow"))).judge_batch(frame(), QUESTIONS)
    assert out.call_failed and out.verdicts == {} and "timed out" in out.error


def test_bad_frame_never_raises():
    out = Judge(llm_with()).judge_batch("not a frame", QUESTIONS)
    assert out.call_failed and out.error
