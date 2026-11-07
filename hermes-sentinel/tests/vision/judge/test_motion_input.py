"""Motion input: ordered, labelled, size-capped frame sequences in one model call."""

from __future__ import annotations

import numpy as np
from harness.judge_helpers import TASK, llm_with
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
motion_mod = plugin_module("sentinel.vision.judge.motion")
Judge = judge_mod.Judge
SequenceFrame = judge_mod.SequenceFrame

YES = {"match": True, "confidence": 0.9, "reason": "moved", "caption": "walks in"}


def noisy(seed: int, w=640, h=480) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)


def sequence(count=3):
    offsets = [-1.0 * (count - 1 - i) for i in range(count)]
    return [SequenceFrame(offset=o, frame=noisy(i)) for i, o in enumerate(offsets)]


def test_sequence_is_sent_in_order_with_relative_labels_in_one_call():
    llm = llm_with(YES)
    frames = sequence(3)
    verdict = Judge(llm).judge(
        frames[-1].frame, "Is someone walking in?", rule_id="r1", sequence=frames
    )
    assert verdict.ok and verdict.match is True
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call.task == TASK and call.purpose == "sentinel:r1"
    assert len(call.images) == 3
    labels = [b.text for b in call.blocks if b.type == "text"]
    assert labels[1:] == [
        "Frame 1 of 3 (t = -2.0 s):",
        "Frame 2 of 3 (t = -1.0 s):",
        "Frame 3 of 3 (CURRENT, t = 0):",
    ]
    instructions = call.kwargs["instructions"]
    assert "time order, oldest first" in instructions and "Is someone walking in?" in instructions


def test_a_single_frame_or_no_sequence_uses_the_still_prompt():
    llm = llm_with(YES, YES)
    judge = Judge(llm)
    judge.judge(noisy(1), "Is the door open?", rule_id="r1")
    judge.judge(noisy(1), "Is the door open?", rule_id="r1", sequence=sequence(1))
    for call in llm.calls:
        assert len(call.images) == 1
        assert "time order" not in call.kwargs["instructions"]


def test_total_sequence_bytes_are_capped():
    frames = [SequenceFrame(offset=-float(2 - i), frame=noisy(i, 1600, 1200)) for i in range(3)]
    encoded = motion_mod.encode_sequence(frames, quality=85, max_bytes=200_000)
    assert sum(len(e.data) for e in encoded) <= 200_000
    assert [e.offset for e in encoded] == sorted(e.offset for e in encoded)


def test_oldest_frames_are_dropped_before_the_cap_is_broken():
    frames = sequence(3)
    encoded = motion_mod.encode_sequence(frames, quality=85, max_bytes=1)
    assert len(encoded) == 1 and encoded[0].offset == 0.0  # the newest frame survives


def test_only_the_newest_frames_are_used():
    frames = [SequenceFrame(offset=-float(5 - i), frame=noisy(i, 64, 48)) for i in range(6)]
    encoded = motion_mod.encode_sequence(frames, quality=85)
    assert [e.offset for e in encoded] == [-2.0, -1.0, 0.0]
