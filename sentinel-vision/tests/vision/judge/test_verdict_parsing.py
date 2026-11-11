"""Defensive verdict parsing: odd replies become values or errors, never exceptions."""

from __future__ import annotations

import pytest
from harness.judge_helpers import frame, llm_with, structured
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
imaging_mod = plugin_module("sentinel.vision.imaging")
Judge = judge_mod.Judge
Example = judge_mod.Example


@pytest.mark.parametrize(
    ("result", "match", "conf"),
    [
        (structured(parsed={"match": "yes", "confidence": "85%"}), True, 0.85),
        (structured(parsed={"match": "No.", "confidence": 70}), False, 0.70),
        (structured(parsed={"match": 1, "confidence": "high"}), True, 0.9),
        (structured(parsed={"answer": "false", "confidence": 2.0}), False, 0.02),
        (structured(text='```json\n{"match": true, "confidence": 0.6}\n```'), True, 0.6),
        (
            structured(text='Sure! {"match": false, "confidence": 0.4, "caption": "x"} ok'),
            False,
            0.4,
        ),
        (structured(parsed={"match": True, "confidence": 1}), True, 1.0),
    ],
)
def test_defensive_parsing(result, match, conf):
    v = Judge(llm_with(result)).judge(frame(), "q?", rule_id="r")
    assert v.ok and v.match is match and v.confidence == pytest.approx(conf)


@pytest.mark.parametrize(
    "result",
    [
        structured(text="I cannot tell."),
        structured(text="{not json"),
        structured(parsed={"confidence": 0.9}),
        structured(parsed={"match": "maybe", "confidence": 0.9}),
        structured(parsed={"match": True}),
        structured(parsed={"match": True, "confidence": -1}),
        structured(parsed={"match": True, "confidence": float("nan")}),
        structured(parsed={"match": True, "confidence": True}),
        structured(text='["a list"]'),
        structured(text=""),
    ],
)
def test_unusable_replies_become_errors(result):
    v = Judge(llm_with(result)).judge(frame(), "q?", rule_id="r")
    assert not v.ok and v.match is None and v.error


def test_long_and_control_text_is_cleaned():
    caption = "a\x00b\nc " + "x" * 1000
    v = Judge(llm_with({"match": True, "confidence": 0.9, "caption": caption})).judge(
        frame(), "q?", rule_id="r"
    )
    assert "\x00" not in v.caption and "\n" not in v.caption
    assert len(v.caption) <= judge_mod.MAX_CAPTION_LEN
