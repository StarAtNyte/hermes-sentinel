"""Judge: request building, error handling and describe."""

from __future__ import annotations

import json

import pytest
from harness.fake_ctx import FakeLlm, PluginLlmTrustError
from harness.judge_helpers import TASK, frame, llm_with, structured
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
imaging_mod = plugin_module("sentinel.vision.imaging")
Judge = judge_mod.Judge
Example = judge_mod.Example


# --------------------------------------------------------------------------- requests


def test_judge_request_shape_and_verdict():
    llm = llm_with(
        {
            "match": True,
            "confidence": 0.92,
            "reason": "mug gone",
            "caption": "a person took the mug",
        }
    )
    examples = [
        Example("pos", b"\xff\xd8\xffP1"),
        Example("pos", b"\xff\xd8\xffP2"),
        Example("pos", b"\xff\xd8\xffP3"),
        Example("neg", b"\xff\xd8\xffN1", note="me"),
    ]
    v = Judge(llm).judge(
        frame(), "Is the mug gone?", rule_id="r1", roi=(0.25, 0.25, 0.5, 0.5), examples=examples
    )
    assert v.ok and v.match is True and v.confidence == pytest.approx(0.92)
    assert v.caption == "a person took the mug" and v.reason == "mug gone" and v.model
    call = llm.calls[-1]
    assert call.task == TASK and call.purpose == "sentinel:r1"
    assert call.kwargs["json_schema"] == judge_mod.VERDICT_SCHEMA
    assert "Is the mug gone?" in call.kwargs["instructions"]
    texts = [b.text for b in call.blocks if b.type == "text"]
    assert texts[:3] == ["Reference example where the answer is YES:"] * 2 + [
        "Reference example where the answer is NO (me):"
    ]
    assert texts[3] == "CURRENT image:" and texts[4].startswith("Close-up")
    assert len(call.images) == 5  # 2 yes + 1 no + current + ROI close-up
    assert call.images[3].mime_type == "image/jpeg"


def test_image_blocks_are_host_dataclasses_with_bytes():
    from agent.plugin_llm import PluginLlmImageInput

    blocks = Judge(None).build_input(frame())
    images = [b for b in blocks if isinstance(b, PluginLlmImageInput)]
    assert len(images) == 1 and isinstance(images[0].data, bytes)


def test_oversized_examples_are_skipped():
    big = Example("pos", b"x" * (judge_mod.MAX_EXAMPLE_BYTES + 1))
    blocks = Judge(None).build_input(frame(), examples=[big, Example("neg", b"")])
    assert len(blocks) == 2  # just the current image


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (PluginLlmTrustError("task not allowed"), "not permitted"),
        (TimeoutError("slow"), "timed out"),
        (RuntimeError("provider down"), "RuntimeError: provider down"),
        (ValueError("schema mismatch"), "ValueError"),
    ],
)
def test_llm_exceptions_never_raise(exc, needle):
    v = Judge(llm_with(exc)).judge(frame(), "q?", rule_id="r")
    assert v.match is None and needle in v.error


def test_schema_mismatch_from_host_is_an_error():
    v = Judge(llm_with({"match": "yes", "confidence": 0.5})).judge(frame(), "q?", rule_id="r")
    assert v.match is None and "ValueError" in v.error  # host validation rejected the type


def test_unregistered_task_is_refused_cleanly():
    llm = FakeLlm(plugin_id="sentinel-vision")  # task never registered
    llm.script({"match": True, "confidence": 1})
    v = Judge(llm).judge(frame(), "q?", rule_id="r")
    assert v.match is None and "not permitted" in v.error
    assert len(llm.rejected) == 1


def test_missing_llm_question_and_frame_errors():
    assert "no LLM" in Judge(None).judge(frame(), "q?", rule_id="r").error
    assert "no LLM" in Judge(lambda: None).judge(frame(), "q?", rule_id="r").error
    assert Judge(llm_with()).judge(frame(), " ", rule_id="r").error == "rule has no question"
    assert Judge(llm_with()).judge("nope", "q?", rule_id="r").error.startswith("bad frame")


def test_llm_provider_callable_is_resolved_per_call():
    llm = llm_with({"match": False, "confidence": 0.8})
    v = Judge(lambda: llm).judge(frame(), "q?", rule_id="r")
    assert v.ok and v.match is False


def test_elapsed_uses_injected_clock():
    ticks = iter([10.0, 12.5])
    v = Judge(llm_with({"match": True, "confidence": 0.9}), clock=lambda: next(ticks)).judge(
        frame(), "q?", rule_id="r"
    )
    assert v.elapsed == pytest.approx(2.5)
    assert json.dumps(v.to_dict())


# --------------------------------------------------------------------------- describe


def test_describe_answers_questions():
    llm = llm_with({"answer": "Two people at the desk.", "caption": "two people"})
    d = Judge(llm).describe(frame(), "Who is there?")
    assert d.ok and d.answer == "Two people at the desk." and d.caption == "two people"
    call = llm.calls[-1]
    assert call.purpose == "sentinel:look" and "Who is there?" in call.kwargs["instructions"]
    assert call.kwargs["json_schema"] == judge_mod.DESCRIBE_SCHEMA


def test_describe_without_question_and_fallbacks():
    d = Judge(llm_with(structured(text="A quiet room."))).describe(frame())
    assert d.ok and d.answer == "A quiet room."
    assert not Judge(llm_with(structured(text=""))).describe(frame()).ok
    assert not Judge(llm_with(structured(parsed={"answer": ""}))).describe(frame()).ok
    assert "RuntimeError" in Judge(llm_with(RuntimeError("x"))).describe(frame()).error
    assert Judge(llm_with()).describe("bad").error.startswith("bad frame")
    d = Judge(llm_with(structured(parsed={"answer": "ok"}))).describe(frame())
    assert d.caption == "ok"
