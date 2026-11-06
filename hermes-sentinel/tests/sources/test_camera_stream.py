"""Camera and stream sources over a fake OpenCV."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import FakeCapture, image, make_fake_cv2

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


def test_camera_lifecycle(fake_cv2: dict[str, Any]) -> None:
    fake_cv2["frames"] = [image(color=(1, 2, 3)), np.zeros((4, 4), np.uint8)]
    clock = iter([100.0, 101.0])
    cam = src.open_source(
        {"kind": "camera", "target": 1, "width": 320, "height": 240}, clock=lambda: next(clock)
    )
    assert isinstance(cam, src.CameraSource)
    assert isinstance(cam, src.FrameSource)
    with pytest.raises(SourceError, match="not open"):
        cam.read()
    cam.open()
    cam.open()  # idempotent
    cap = FakeCapture.instances[0]
    assert len(FakeCapture.instances) == 1
    assert cap.target == 1
    assert cap.props == {38: 1, 3: 320, 4: 240}
    ts, frame = cam.read()
    assert ts == 100.0 and tuple(frame[0, 0]) == (1, 2, 3)
    ts, frame = cam.read()
    assert ts == 101.0 and frame.shape == (4, 4, 3)
    with pytest.raises(SourceDisconnected):
        cam.read()
    assert not cam.is_open and cap.released == 1
    cam.close()
    cam.close()
    assert cap.released == 1
    assert "camera:1" in repr(cam)


def test_camera_read_exception_is_disconnect(fake_cv2: dict[str, Any]) -> None:
    fake_cv2["frames"] = [RuntimeError("driver")]
    with src.open_source("camera:0") as cam, pytest.raises(SourceDisconnected, match="driver"):
        cam.read()


def test_camera_not_opened(fake_cv2: dict[str, Any]) -> None:
    fake_cv2["opened"] = False
    cam = src.open_source("camera:3")
    with pytest.raises(SourceDisconnected, match="cannot open"):
        cam.open()
    assert FakeCapture.instances[0].released == 1
    assert not cam.is_open


def test_camera_constructor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(*a: Any) -> Any:
        raise RuntimeError("v4l2 exploded")

    monkeypatch.setattr(deps, "require_cv2", lambda p: make_fake_cv2(factory))
    with pytest.raises(SourceDisconnected, match="v4l2 exploded"):
        src.open_source("camera:0").open()


def test_camera_open_timeout_releases_late_capture(fake_cv2: dict[str, Any]) -> None:
    gate = threading.Event()
    fake_cv2["block"] = gate
    cam = src.open_source({"spec": "camera:0", "timeout": 0.1})
    start = time.monotonic()
    with pytest.raises(SourceDisconnected, match="timed out"):
        cam.open()
    assert time.monotonic() - start < 2
    gate.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not (
        FakeCapture.instances and FakeCapture.instances[0].released
    ):
        time.sleep(0.01)
    assert FakeCapture.instances[0].released == 1
    assert not cam.is_open


def test_wrong_spec_kind_rejected() -> None:
    cam = SourceSpec(kind="camera", target=0)
    for cls in (src.StreamSource, src.SnapshotUrlSource, src.ScreenSource, src.FolderSource):
        with pytest.raises(SourceSpecError):
            cls(cam)
    with pytest.raises(SourceSpecError):
        src.CameraSource(SourceSpec(kind="screen", target=1))


def test_close_waits_for_inflight_read(fake_cv2: dict[str, Any]) -> None:
    """close() from another thread must not release the capture under a running read."""
    entered, finish = threading.Event(), threading.Event()
    events: list[str] = []

    class SlowCapture(FakeCapture):
        def read(self) -> tuple[bool, Any]:
            entered.set()
            finish.wait(2)
            events.append("read-done")
            return True, image()

        def release(self) -> None:
            events.append("release")
            super().release()

    fake_cv2["module"].VideoCapture = lambda t, *a: SlowCapture(t)
    cam = src.open_source("camera:0")
    cam.open()
    reader = threading.Thread(target=cam.read)
    reader.start()
    assert entered.wait(2)
    closer = threading.Thread(target=cam.close)
    closer.start()
    time.sleep(0.05)
    finish.set()
    reader.join(2)
    closer.join(2)
    assert events == ["read-done", "release"]


# ----------------------------------------------------------------------------- stream


def test_stream_resolves_secret_placeholders(fake_cv2: dict[str, Any], shim_binding) -> None:
    shim_binding.secrets["CAM_PW"] = "p@ss/word"
    stream = src.open_source("rtsp://admin:${CAM_PW}@cam.local/live", clock=lambda: 5.0)
    assert isinstance(stream, src.StreamSource)
    stream.open()
    cap = FakeCapture.instances[0]
    assert cap.target == "rtsp://admin:p%40ss%2Fword@cam.local/live"
    assert cap.args[0] == 1900
    assert cap.args[1] == [53, 10000, 54, 10000]
    assert "CAM_PW" in shim_binding.secrets_read
    assert "p@ss" not in repr(stream) and "p@ss" not in stream.name
    ts, frame = stream.read()
    assert ts == 5.0 and frame.shape == (48, 64, 3)
    stream.close()


def test_stream_missing_secret(fake_cv2: dict[str, Any], shim_binding) -> None:
    stream = src.open_source("rtsp://admin:${NOPE}@cam/live")
    with pytest.raises(SourceError, match="NOPE"):
        stream.open()
    assert not stream.is_open


def test_stream_falls_back_without_params_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def factory(*args: Any) -> FakeCapture:
        calls.append(args)
        if len(args) == 3:
            raise TypeError("no overload")
        return FakeCapture(args[0])

    monkeypatch.setattr(deps, "require_cv2", lambda p: make_fake_cv2(factory))
    with src.open_source("stream:http://cam/v.mjpg"):
        pass
    assert [len(c) for c in calls] == [3, 2]


def test_secret_scope_failure_is_source_error(
    monkeypatch: pytest.MonkeyPatch, fake_cv2: dict[str, Any]
) -> None:
    import agent.secret_scope as scope

    def raising(name: str, default: Any = None) -> Any:
        raise RuntimeError("multiplex scope missing")

    monkeypatch.setattr(scope, "get_secret", raising)
    with pytest.raises(SourceError, match="RuntimeError"):
        src.open_source("rtsp://u:${X}@cam/live").open()
