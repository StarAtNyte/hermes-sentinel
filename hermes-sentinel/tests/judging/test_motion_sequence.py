"""Choosing the earlier frames a motion rule is shown."""

from __future__ import annotations

from types import SimpleNamespace

from harness.plugin_loader import plugin_module

motion_sequence = plugin_module("sentinel.judging.motion").motion_sequence


def recent(*stamps):
    return [SimpleNamespace(ts=t, frame=f"f{t}") for t in stamps]


def test_offsets_are_relative_and_the_current_frame_is_last():
    seq = motion_sequence(recent(8.0, 9.0), "now", 10.0, 3)
    assert [s.offset for s in seq] == [-2.0, -1.0, 0.0]
    assert [s.frame for s in seq] == ["f8.0", "f9.0", "now"]


def test_only_the_newest_earlier_frames_fit():
    seq = motion_sequence(recent(5.0, 8.0, 9.0), "now", 10.0, 3)
    assert [s.frame for s in seq] == ["f8.0", "f9.0", "now"]
    assert len(motion_sequence(recent(8.0, 9.0), "now", 10.0, 2)) == 2


def test_frames_too_close_to_the_current_one_are_skipped():
    assert motion_sequence(recent(9.9, 10.0), "now", 10.0, 3) == ()


def test_nothing_earlier_or_too_few_frames_means_a_still():
    assert motion_sequence([], "now", 10.0, 3) == ()
    assert motion_sequence(recent(8.0), "now", 10.0, 1) == ()
    assert motion_sequence(recent(8.0), "now", 10.0, 0) == ()
