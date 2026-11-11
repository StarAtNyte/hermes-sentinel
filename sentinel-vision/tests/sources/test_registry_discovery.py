"""Source factory and the camera/monitor probe."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import FakeCapture, FakeMSS, image, make_fake_cv2, write_image

src = plugin_module("sentinel.sources")
deps = plugin_module("sentinel.sources.dependencies")
timeouts = plugin_module("sentinel.sources.timeouts")
frames = plugin_module("sentinel.sources.frames")
screen = plugin_module("sentinel.sources.backends.screen")
discovery = plugin_module("sentinel.sources.discovery")
SourceSpec = src.SourceSpec
SourceSpecError = src.SourceSpecError
SourceError = src.SourceError
SourceDisconnected = src.SourceDisconnected
SourceNotReady = src.SourceNotReady
MissingDependency = src.MissingDependency


def test_open_source_auto_open(tmp_path: Path) -> None:
    write_image(tmp_path / "a.png", image(), time.time())
    folder = src.open_source(str(tmp_path), auto_open=True)
    assert folder.is_open
    folder.close()


def test_open_source_named_stream(fake_cv2: dict[str, Any]) -> None:
    s = src.open_source("door", named_streams={"door": "rtsp://cam/live"})
    assert isinstance(s, src.StreamSource) and s.name == "door"


def test_open_source_bad_spec() -> None:
    with pytest.raises(SourceSpecError):
        src.open_source("???")


def test_list_sources(monkeypatch: pytest.MonkeyPatch, fake_mss, probe_cleanup) -> None:
    def factory(index: Any, *a: Any) -> FakeCapture:
        return FakeCapture(index, opened=index in (0, 2))

    monkeypatch.setattr(deps, "require_cv2", lambda p: make_fake_cv2(factory))
    infos = src.list_sources(
        named_streams={
            "door": "rtsp://u:${PW}@cam/live",
            "porch": "http://cam/s.jpg",
            "broken": "ftp://x",
        },
        max_camera_index=3,
    )
    by_spec = {i.spec: i for i in infos}
    assert by_spec["camera:0"].available is True
    assert by_spec["camera:0"].detail == "640x480"
    assert by_spec["camera:1"].available is False
    assert by_spec["camera:2"].available is True
    assert "camera:4" not in by_spec
    assert by_spec["screen:0"].name == "all monitors"
    assert by_spec["screen:2"].detail == "100x80"
    assert by_spec["door"].available is None and "${PW}" in by_spec["door"].detail
    assert by_spec["porch"].kind == "snapshot"
    assert by_spec["stream:broken"].available is False
    assert all(c.released for c in FakeCapture.instances)
    d = by_spec["camera:0"].to_dict()
    assert d["width"] == 640 and d["available"] is True


def test_list_sources_never_hangs(monkeypatch: pytest.MonkeyPatch, fake_mss, probe_cleanup) -> None:
    gate = threading.Event()
    probe_cleanup.append(gate)

    def factory(index: Any, *a: Any) -> FakeCapture:
        if index == 1:
            gate.wait(10)
        return FakeCapture(index, opened=True)

    monkeypatch.setattr(deps, "require_cv2", lambda p: make_fake_cv2(factory))
    start = time.monotonic()
    infos = src.list_sources(
        max_camera_index=2, total_timeout=0.5, per_camera_timeout=0.2, probe_screens=False
    )
    assert time.monotonic() - start < 2
    by_spec = {i.spec: i for i in infos}
    assert by_spec["camera:0"].available is True
    assert by_spec["camera:1"].available is None and "timed out" in by_spec["camera:1"].detail
    # A second probe while the first is stuck does not start another thread.
    again = src.list_sources(max_camera_index=1, total_timeout=0.3, probe_screens=False)
    assert all("still running" in i.detail for i in again)
    gate.set()
    discovery._probe_thread.join(5)
    assert not discovery._probe_thread.is_alive()


def test_list_sources_camera_error(monkeypatch: pytest.MonkeyPatch, probe_cleanup) -> None:
    def factory(index: Any, *a: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(deps, "require_cv2", lambda p: make_fake_cv2(factory))
    infos = src.list_sources(max_camera_index=0, probe_screens=False)
    assert infos[0].available is False and "boom" in infos[0].detail


def test_list_sources_screen_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_display() -> Any:
        raise RuntimeError("no display")

    monkeypatch.setattr(screen, "mss_factory", lambda p: (no_display, RuntimeError))
    infos = src.list_sources(probe_cameras=False)
    assert infos[0].available is False and "no display" in infos[0].detail

    gate = threading.Event()

    class SlowMSS(FakeMSS):
        def __init__(self) -> None:
            gate.wait(5)
            super().__init__()

    monkeypatch.setattr(screen, "mss_factory", lambda p: (SlowMSS, RuntimeError))
    start = time.monotonic()
    infos = src.list_sources(probe_cameras=False, total_timeout=0.3)
    gate.set()
    assert time.monotonic() - start < 2
    assert infos[0].available is None and "timed out" in infos[0].detail


def test_list_sources_clamps_camera_range(monkeypatch: pytest.MonkeyPatch, probe_cleanup) -> None:
    monkeypatch.setattr(
        deps, "require_cv2", lambda p: make_fake_cv2(lambda i, *a: FakeCapture(i, opened=False))
    )
    infos = src.list_sources(max_camera_index=-5, probe_screens=False)
    assert [i.spec for i in infos] == ["camera:0"]
