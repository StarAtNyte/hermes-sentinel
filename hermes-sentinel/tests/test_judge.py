"""Judge: image encoding, request building and defensive verdict parsing (never raises)."""

from __future__ import annotations

import json

import cv2
import numpy as np
import pytest
from harness.fake_ctx import FakeLlm, PluginLlmStructuredResult, PluginLlmTrustError
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.judge")
Judge = judge_mod.Judge
Example = judge_mod.Example
TASK = judge_mod.VISION_TASK


def frame(w=320, h=240, value=128):
    img = np.full((h, w, 3), value, dtype=np.uint8)
    img[h // 4: h // 2, w // 4: w // 2] = (40, 40, 200)
    return img


def llm_with(*responses):
    llm = FakeLlm(plugin_id="hermes-sentinel", owned_tasks=lambda: frozenset({TASK}))
    llm.script(*responses)
    return llm


def structured(text="", parsed=None, model="m"):
    return PluginLlmStructuredResult(text=text, provider="p", model=model, agent_id="a",
                                     parsed=parsed)


# --------------------------------------------------------------------------- encoding


def test_encode_jpeg_limits_longest_side():
    data, mime = judge_mod.encode_image(np.zeros((1500, 2000, 3), np.uint8))
    assert mime == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (768, 1024)


def test_small_frames_are_not_upscaled():
    data, _ = judge_mod.encode_image(frame(160, 120))
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (120, 160)


@pytest.mark.parametrize(
    "arr",
    [
        np.full((40, 50), 7, np.uint8),
        np.full((40, 50, 1), 7, np.uint8),
        np.full((40, 50, 4), 7, np.uint8),
        np.full((40, 50, 3), 0.5, np.float32),
        np.full((40, 50, 3), 1000, np.uint16),
        np.full((40, 50, 3), np.nan, np.float64),
    ],
)
def test_encode_accepts_common_layouts(arr):
    data, mime = judge_mod.encode_image(arr)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (40, 50, 3)


@pytest.mark.parametrize("bad", [np.zeros((4,)), np.zeros((0, 5, 3)), np.zeros((5, 5, 2)), "x"])
def test_encode_rejects_unusable_frames(bad):
    with pytest.raises(ValueError):
        judge_mod.encode_image(bad)
    assert judge_mod.encode_jpeg(bad) is None


def test_png_fallback_without_cv2(monkeypatch):
    monkeypatch.setattr(judge_mod, "_cv2", lambda: None)
    src = np.random.default_rng(0).integers(0, 255, (30, 41, 3), dtype=np.uint8)
    data, mime = judge_mod.encode_image(src)
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(decoded, src)  # lossless, BGR order preserved
    assert judge_mod.encode_jpeg(src) is None  # storage needs JPEG


def test_numpy_downscale_fallback(monkeypatch):
    monkeypatch.setattr(judge_mod, "_cv2", lambda: None)
    data, _ = judge_mod.encode_image(np.zeros((900, 3000, 3), np.uint8), max_side=1024)
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) <= 1024


def test_crop_roi():
    img = frame(200, 100)
    assert judge_mod.crop_roi(img, None) is None
    assert judge_mod.crop_roi(img, (0, 0, 1, 1)) is None  # most of the frame: no close-up
    crop = judge_mod.crop_roi(img, (0.5, 0.5, 0.5, 0.5))
    assert crop.shape == (50, 100, 3)
    assert judge_mod.crop_roi(img, (0.0, 0.0, 0.01, 0.01)) is None  # too small
    assert judge_mod.crop_roi(img, ("a", 0, 1, 1)) is None
    assert judge_mod.crop_roi(img, (0, 0, float("nan"), 0.2)) is None


# --------------------------------------------------------------------------- requests


def test_judge_request_shape_and_verdict():
    llm = llm_with({"match": True, "confidence": 0.92, "reason": "mug gone",
                    "caption": "a person took the mug"})
    examples = [Example("pos", b"\xff\xd8\xffP1"), Example("pos", b"\xff\xd8\xffP2"),
                Example("pos", b"\xff\xd8\xffP3"), Example("neg", b"\xff\xd8\xffN1", note="me")]
    v = Judge(llm).judge(frame(), "Is the mug gone?", rule_id="r1", roi=(0.25, 0.25, 0.5, 0.5),
                         examples=examples)
    assert v.ok and v.match is True and v.confidence == pytest.approx(0.92)
    assert v.caption == "a person took the mug" and v.reason == "mug gone" and v.model
    call = llm.calls[-1]
    assert call.task == TASK and call.purpose == "sentinel:r1"
    assert call.kwargs["json_schema"] == judge_mod.VERDICT_SCHEMA
    assert "Is the mug gone?" in call.kwargs["instructions"]
    texts = [b.text for b in call.blocks if b.type == "text"]
    assert texts[:3] == ["Reference example where the answer is YES:"] * 2 + [
        "Reference example where the answer is NO (me):"]
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
    ("result", "match", "conf"),
    [
        (structured(parsed={"match": "yes", "confidence": "85%"}), True, 0.85),
        (structured(parsed={"match": "No.", "confidence": 70}), False, 0.70),
        (structured(parsed={"match": 1, "confidence": "high"}), True, 0.9),
        (structured(parsed={"answer": "false", "confidence": 2.0}), False, 0.02),
        (structured(text='```json\n{"match": true, "confidence": 0.6}\n```'), True, 0.6),
        (structured(text='Sure! {"match": false, "confidence": 0.4, "caption": "x"} ok'),
         False, 0.4),
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
    llm = FakeLlm(plugin_id="hermes-sentinel")  # task never registered
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


def test_long_and_control_text_is_cleaned():
    caption = "a\x00b\nc " + "x" * 1000
    v = Judge(llm_with({"match": True, "confidence": 0.9, "caption": caption})).judge(
        frame(), "q?", rule_id="r")
    assert "\x00" not in v.caption and "\n" not in v.caption
    assert len(v.caption) <= judge_mod.MAX_CAPTION_LEN


def test_elapsed_uses_injected_clock():
    ticks = iter([10.0, 12.5])
    v = Judge(llm_with({"match": True, "confidence": 0.9}), clock=lambda: next(ticks)).judge(
        frame(), "q?", rule_id="r")
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
