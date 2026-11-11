"""Video-file source: spec parsing, validation and playback of a tiny generated video."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from harness.plugin_loader import plugin_module

src = plugin_module("sentinel.sources")
SourceSpec = src.SourceSpec
SourceSpecError = src.SourceSpecError

cv2 = pytest.importorskip("cv2")
FRAME_COUNT = 6


@pytest.fixture
def video_path(tmp_path: Path) -> Path:
    """A 6-frame MJPG .avi whose frame brightness rises with the frame number."""
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (32, 24))
    assert writer.isOpened()
    for index in range(FRAME_COUNT):
        writer.write(np.full((24, 32, 3), 20 + index * 40, np.uint8))
    writer.release()
    return path


def _levels(source, count: int) -> list[int]:
    return [int(source.read()[1].mean() // 20) for _ in range(count)]


# ------------------------------------------------------------------- spec parsing


@pytest.mark.parametrize("text", ["file:/tmp/a.mp4", "/tmp/a.mp4", "/tmp/a.MKV", "x.mov", "a.avi"])
def test_file_specs_and_bare_video_paths_parse(text):
    spec = SourceSpec.parse(text)
    assert spec.kind == "file" and spec.loop is False and spec.speed == 1


def test_file_url_form_and_options_round_trip():
    spec = SourceSpec.parse("file:///tmp/a.mp4?loop=true&speed=3")
    assert (spec.target, spec.loop, spec.speed) == ("/tmp/a.mp4", True, 3)
    assert spec.canonical() == "file:/tmp/a.mp4?loop=true&speed=3"
    assert SourceSpec.parse(spec.canonical()) == spec


def test_question_mark_in_path_is_not_an_option():
    assert SourceSpec.parse("file:/tmp/a?b.mp4").target == "/tmp/a?b.mp4"


def test_mapping_options():
    spec = SourceSpec.parse({"spec": "file:/tmp/a.mp4", "loop": True, "speed": 2})
    assert (spec.loop, spec.speed) == (True, 2)
    assert spec.to_dict()["speed"] == 2


@pytest.mark.parametrize(
    "text",
    [
        "file:http://host/a.mp4",
        "file:/tmp/a.txt",
        "file:/tmp/a.mp4?speed=0",
        "file:/tmp/a.mp4?loop=maybe",
        "file:",
    ],
)
def test_bad_file_specs_are_rejected(text):
    with pytest.raises(SourceSpecError):
        SourceSpec.parse(text)


def test_remote_video_urls_stay_streams():
    assert SourceSpec.parse("https://host/live.mp4").kind != "file"
    assert SourceSpec.parse("rtsp://host/a.mp4").kind == "stream"


def test_loop_and_speed_rejected_on_other_kinds():
    with pytest.raises(SourceSpecError):
        SourceSpec(kind="camera", target=0, loop=True)


# ------------------------------------------------------------------- playback


def test_plays_to_the_end_then_reports_exhausted(video_path):
    source = src.open_source(f"file:{video_path}")
    source.open()
    levels = _levels(source, FRAME_COUNT)
    assert levels == sorted(levels) and levels[0] < levels[-1]
    with pytest.raises(src.SourceExhausted):
        source.read()
    assert source.is_open is False


def test_loop_restarts_at_the_beginning(video_path):
    source = src.open_source(f"file:{video_path}?loop=true")
    source.open()
    levels = _levels(source, FRAME_COUNT * 2 + 1)
    assert levels[:FRAME_COUNT] == levels[FRAME_COUNT : FRAME_COUNT * 2]
    assert levels[-1] == levels[0]
    source.close()


def test_speed_is_a_frame_step(video_path):
    source = src.open_source(f"file:{video_path}?speed=3")
    source.open()
    assert len(_levels(source, 2)) == 2  # frames 0 and 3
    with pytest.raises(src.SourceExhausted):
        source.read()


def test_missing_file_is_disconnected_not_fatal(tmp_path):
    source = src.open_source(f"file:{tmp_path / 'nope.mp4'}")
    with pytest.raises(src.SourceDisconnected, match="does not exist"):
        source.open()


def test_directory_named_like_a_video_is_refused(tmp_path):
    folder = tmp_path / "fake.mp4"
    folder.mkdir()
    with pytest.raises(SourceSpecError, match="regular file"):
        src.open_source(f"file:{folder}").open()


def test_file_source_needs_a_file_spec():
    with pytest.raises(SourceSpecError):
        src.FileSource(SourceSpec.parse("camera:0"))
