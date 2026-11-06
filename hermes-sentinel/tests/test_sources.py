"""Unit tests for sentinel.sources: specs, every source kind (with fakes), the probe, the factory.

FolderSource and the snapshot decoder run for real against generated images; OpenCV
``VideoCapture`` and mss are replaced by fakes so no camera or display is needed.
"""

from __future__ import annotations

import ast
import base64
import http.server
import io
import os
import threading
import time
import types
import urllib.error
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from harness.plugin_loader import PLUGIN_DIR, plugin_module

src = plugin_module("sentinel.sources")
SourceSpec = src.SourceSpec
SourceSpecError = src.SourceSpecError
SourceError = src.SourceError
SourceDisconnected = src.SourceDisconnected
SourceNotReady = src.SourceNotReady
MissingDependency = src.MissingDependency


# ----------------------------------------------------------------------------- helpers


def _image(h: int = 48, w: int = 64, color: tuple[int, int, int] = (10, 20, 30)) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def _png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


class FakeCapture:
    """Stand-in for cv2.VideoCapture."""

    instances: list[FakeCapture] = []

    def __init__(self, target: Any, *args: Any, frames: list[Any] | None = None,
                 opened: bool = True, block: threading.Event | None = None) -> None:
        if block is not None:
            block.wait(10)
        self.target = target
        self.args = args
        self.frames = list(frames) if frames is not None else [_image()]
        self.opened = opened
        self.released = 0
        self.props: dict[int, float] = {}
        FakeCapture.instances.append(self)

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2
        return self.opened

    def read(self) -> tuple[bool, Any]:
        if not self.frames:
            return False, None
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return True, item

    def set(self, prop: int, value: float) -> bool:
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return {3: 640.0, 4: 480.0}.get(prop, 0.0)

    def release(self) -> None:
        self.released += 1
        self.opened = False


def make_fake_cv2(factory) -> types.ModuleType:
    mod = types.ModuleType("cv2_fake")
    mod.VideoCapture = factory
    mod.CAP_PROP_BUFFERSIZE = 38
    mod.CAP_PROP_FRAME_WIDTH = 3
    mod.CAP_PROP_FRAME_HEIGHT = 4
    mod.CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    mod.CAP_PROP_READ_TIMEOUT_MSEC = 54
    mod.CAP_FFMPEG = 1900
    mod.IMREAD_COLOR = cv2.IMREAD_COLOR
    mod.imdecode = cv2.imdecode
    return mod


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeCapture.instances = []
    yield
    FakeCapture.instances = []


@pytest.fixture
def fake_cv2(monkeypatch: pytest.MonkeyPatch):
    """Install a fake cv2 whose VideoCapture is configurable via ``state``."""
    state: dict[str, Any] = {"frames": None, "opened": True, "block": None}

    def factory(target: Any, *args: Any) -> FakeCapture:
        return FakeCapture(target, *args, frames=state["frames"], opened=state["opened"],
                           block=state["block"])

    mod = make_fake_cv2(factory)
    monkeypatch.setattr(src, "_cv2", lambda purpose: mod)
    state["module"] = mod
    return state


# ----------------------------------------------------------------------------- module hygiene


def test_optional_deps_not_imported_at_module_level() -> None:
    tree = ast.parse((PLUGIN_DIR / "sentinel" / "sources.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not names & {"cv2", "mss", "numpy", "agent"}, names


def test_no_environ_reads_in_sources() -> None:
    tree = ast.parse((PLUGIN_DIR / "sentinel" / "sources.py").read_text())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"environ", "getenv", "environb"}


def test_missing_dependency_has_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    real_import = src.importlib.import_module

    def fake_import(name: str, *a: Any, **k: Any):
        if name in ("cv2", "mss"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(src.importlib, "import_module", fake_import)
    with pytest.raises(MissingDependency) as exc:
        src.open_source("camera:0").open()
    assert "opencv-python-headless" in str(exc.value)
    assert isinstance(exc.value, SourceError) and isinstance(exc.value, ImportError)
    with pytest.raises(MissingDependency, match="'mss'"):
        src.open_source("screen").open()
    with pytest.raises(MissingDependency):
        src.open_source(f"folder:{tmp_path}").open()
    status = src.dependency_status()
    assert status["cv2"] and "opencv-python-headless" in status["cv2"]
    assert status["numpy"] is None
    # The probe reports the hint instead of raising.
    infos = src.list_sources(total_timeout=2)
    assert any("opencv-python-headless" in i.detail for i in infos if i.kind == "camera")
    assert any("mss" in i.detail for i in infos if i.kind == "screen")


# ----------------------------------------------------------------------------- spec parsing


@pytest.mark.parametrize(
    ("text", "kind", "target"),
    [
        ("0", "camera", 0),
        ("camera:2", "camera", 2),
        ("webcam", "camera", 0),
        ("cam:1", "camera", 1),
        ("screen", "screen", 1),
        ("screen:0", "screen", 0),
        ("monitor:2", "screen", 2),
        ("rtsp://cam.local/live", "stream", "rtsp://cam.local/live"),
        ("http://cam.local/video.mjpg", "stream", "http://cam.local/video.mjpg"),
        ("https://cam.local/hls/index.m3u8", "stream", "https://cam.local/hls/index.m3u8"),
        ("stream:http://cam.local/x", "stream", "http://cam.local/x"),
        ("http://cam.local/snap.jpg", "snapshot", "http://cam.local/snap.jpg"),
        ("snapshot:http://cam.local/v.mjpg", "snapshot", "http://cam.local/v.mjpg"),
        ("folder:/tmp/x", "folder", "/tmp/x"),
    ],
)
def test_parse_strings(text: str, kind: str, target: Any) -> None:
    spec = SourceSpec.parse(text)
    assert spec.kind == kind
    assert spec.target == target
    assert SourceSpec.parse(spec.canonical()) == spec


def test_parse_int_and_existing_spec_and_directory(tmp_path: Path) -> None:
    assert SourceSpec.parse(3) == SourceSpec(kind="camera", target=3)
    spec = SourceSpec(kind="screen", target=1)
    assert SourceSpec.parse(spec) is spec
    folder = SourceSpec.parse(str(tmp_path))
    assert folder.kind == "folder" and folder.target == str(tmp_path)


def test_parse_screen_region() -> None:
    spec = SourceSpec.parse("screen:1@100, 200,640,480")
    assert spec.region == (100, 200, 640, 480)
    assert spec.canonical() == "screen:1@100,200,640,480"
    assert SourceSpec.parse("screen:@0,0,10,10").target == 1


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "nonsense",
        "camera:x",
        "camera:-1",
        "camera:999",
        "screen:1@1,2,3",
        "screen:1@0,0,0,5",
        "ftp://host/x",
        "rtsp://admin:hunter2@cam/live",
        "http://cam/snap.jpg?password=hunter2",
        "http://cam/snap.jpg?token=abc",
        "http://",
        "stream:file:///etc/passwd",
        "snapshot:rtsp://cam/x",
        "http://cam/x\r\nHost: evil",
        "folder:",
    ],
)
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(SourceSpecError):
        SourceSpec.parse(bad)


def test_parse_rejects_non_string_types() -> None:
    for bad in (True, 1.5, None, [1]):
        with pytest.raises(SourceSpecError):
            SourceSpec.parse(bad)  # type: ignore[arg-type]


def test_spec_error_is_value_error_and_hides_password() -> None:
    with pytest.raises(ValueError) as exc:
        SourceSpec.parse("rtsp://admin:hunter2@cam/live")
    assert "hunter2" not in str(exc.value)
    with pytest.raises(SourceSpecError) as exc2:
        SourceSpec.parse("nope://admin:hunter2@cam/live")
    assert "hunter2" not in str(exc2.value)


def test_placeholders_allowed_in_url() -> None:
    spec = SourceSpec.parse("rtsp://admin:${CAM_PASS}@cam/live?token=${CAM_TOKEN}")
    assert spec.kind == "stream"
    assert "${CAM_PASS}" in spec.canonical()


def test_username_without_password_allowed() -> None:
    assert SourceSpec.parse("rtsp://viewer@cam/live").kind == "stream"


def test_named_streams() -> None:
    named = {
        "door": "rtsp://u:${DOOR_PW}@10.0.0.5/live",
        "porch": "http://10.0.0.6/snapshot.jpg",
        "yard": "snapshot:http://10.0.0.7/cgi",
        "garage": "stream:http://10.0.0.8/cgi",
    }
    door = SourceSpec.parse("door", named_streams=named)
    assert (door.kind, door.name) == ("stream", "door")
    assert SourceSpec.parse("stream:door", named_streams=named) == door
    assert SourceSpec.parse("porch", named_streams=named).kind == "snapshot"
    assert SourceSpec.parse("yard", named_streams=named).kind == "snapshot"
    assert SourceSpec.parse("garage", named_streams=named).kind == "stream"
    assert door.display_name == "door"
    with pytest.raises(SourceSpecError):
        SourceSpec.parse("bad", named_streams={"bad": 5})  # type: ignore[dict-item]


def test_parse_mapping_forms() -> None:
    spec = SourceSpec.parse({"kind": "screen", "monitor": "2", "region": "0,0,100,50",
                             "name": "chart"})
    assert spec == SourceSpec(kind="screen", target=2, region=(0, 0, 100, 50), name="chart")
    spec = SourceSpec.parse({"spec": "camera:1", "width": 1280, "height": 720, "timeout": 3})
    assert (spec.target, spec.width, spec.height, spec.timeout) == (1, 1280, 720, 3.0)
    spec = SourceSpec.parse({"kind": "snapshot", "url": "https://cam/s.jpg",
                             "username_secret": "CAM_USER", "password_secret": "CAM_PASS"})
    assert spec.username_secret == "CAM_USER"
    spec = SourceSpec.parse({"kind": "stream", "target": "door"},
                            named_streams={"door": "rtsp://cam/live"})
    assert spec.target == "rtsp://cam/live"
    spec = SourceSpec.parse({"kind": "screen", "target": 1, "region": [1, 2, 3, 4]})
    assert spec.region == (1, 2, 3, 4)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "camera"},
        {"target": 1},
        {"kind": "camera", "target": 0, "bogus": 1},
        {"kind": "laser", "target": 0},
        {"kind": "camera", "target": 0, "region": [0, 0, 1, 1]},
        {"kind": "screen", "target": 1, "region": ["a", 0, 1, 1]},
        {"kind": "camera", "target": 0, "token_secret": "X"},
        {"kind": "snapshot", "target": "http://c/s", "username_secret": "U"},
        {"kind": "snapshot", "target": "http://c/s", "token_secret": "bad name"},
        {"kind": "snapshot", "target": "http://c/s", "token_secret": "T",
         "username_secret": "U", "password_secret": "P"},
        {"kind": "camera", "target": 0, "width": 0},
        {"kind": "camera", "target": 0, "width": 99999},
        {"kind": "camera", "target": True},
        {"kind": "folder", "target": "a\x00b"},
    ],
)
def test_parse_mapping_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(SourceSpecError):
        SourceSpec.parse(bad)


def test_timeout_is_clamped() -> None:
    assert SourceSpec(kind="camera", target=0, timeout=-1).timeout == src.DEFAULT_TIMEOUT_S
    assert SourceSpec(kind="camera", target=0, timeout=float("nan")).timeout == 10.0
    assert SourceSpec(kind="camera", target=0, timeout=1e9).timeout == src.MAX_TIMEOUT_S
    assert SourceSpec(kind="camera", target=0, timeout="abc").timeout == 10.0  # type: ignore[arg-type]


def test_to_dict_is_json_safe_and_redacted() -> None:
    spec = SourceSpec.parse({"kind": "snapshot", "url": "https://u:${P}@cam/s.jpg",
                             "token_secret": "CAM_TOKEN"})
    d = spec.to_dict()
    assert d["kind"] == "snapshot" and d["token_secret"] == "CAM_TOKEN"
    assert d["spec"].startswith("snapshot:https://u:${P}@cam")
    assert SourceSpec(kind="screen", target=1, region=(0, 0, 5, 5)).to_dict()["region"] == [
        0, 0, 5, 5]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("rtsp://u:secret@h/p", "rtsp://u:***@h/p"),
        ("rtsp://u:${PW}@h/p", "rtsp://u:${PW}@h/p"),
        ("http://h/p?token=abc&x=1", "http://h/p?token=%2A%2A%2A&x=1"),
        ("http://h/p?key=${K}", "http://h/p?key=${K}"),
        ("http://h/p", "http://h/p"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert src.redact_url(url) == expected


def test_redact_url_invalid() -> None:
    assert src.redact_url("http://[::1") == "<invalid url>"


# ----------------------------------------------------------------------------- timeout helper


def test_call_with_timeout_returns_and_raises() -> None:
    assert src._call_with_timeout(lambda: 5, 1.0) == 5

    def boom() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        src._call_with_timeout(boom, 1.0)


def test_call_with_timeout_abandons_and_cleans_up() -> None:
    gate = threading.Event()
    cleaned = threading.Event()
    seen: list[Any] = []

    def slow() -> str:
        gate.wait(5)
        return "late"

    def cleanup(value: str) -> None:
        seen.append(value)
        cleaned.set()

    start = time.monotonic()
    with pytest.raises(src._Timeout):
        src._call_with_timeout(slow, 0.1, on_abandoned=cleanup)
    assert time.monotonic() - start < 1.0
    gate.set()
    assert cleaned.wait(2)
    assert seen == ["late"]


# ----------------------------------------------------------------------------- frames


def test_as_bgr_conversions() -> None:
    gray = np.full((4, 5), 7, dtype=np.uint8)
    out = src._as_bgr(gray, origin="t")
    assert out.shape == (4, 5, 3) and out.dtype == np.uint8
    one = np.zeros((4, 5, 1), dtype=np.uint8)
    assert src._as_bgr(one, origin="t").shape == (4, 5, 3)
    bgra = np.zeros((4, 5, 4), dtype=np.uint8)
    bgra[..., 3] = 255
    out = src._as_bgr(bgra, origin="t")
    assert out.shape == (4, 5, 3) and out.flags.c_contiguous
    deep = np.full((2, 2, 3), 0xFFFF, dtype=np.uint16)
    assert src._as_bgr(deep, origin="t").max() == 255
    for bad in (None, np.zeros((0, 0, 3), np.uint8), np.zeros((2, 2, 3), np.float32),
                np.zeros((2, 2, 2), np.uint8), np.zeros((2,), np.uint8)):
        with pytest.raises(SourceError):
            src._as_bgr(bad, origin="t")


def test_decode_image_errors() -> None:
    with pytest.raises(SourceError):
        src._decode_image(b"", origin="t")
    with pytest.raises(SourceError, match="not a decodable"):
        src._decode_image(b"not an image", origin="t")
    img = src._decode_image(_png_bytes(_image(color=(1, 2, 3))), origin="t")
    assert tuple(img[0, 0]) == (1, 2, 3)


# ----------------------------------------------------------------------------- camera


def test_camera_lifecycle(fake_cv2: dict[str, Any]) -> None:
    fake_cv2["frames"] = [_image(color=(1, 2, 3)), np.zeros((4, 4), np.uint8)]
    clock = iter([100.0, 101.0])
    cam = src.open_source({"kind": "camera", "target": 1, "width": 320, "height": 240},
                          clock=lambda: next(clock))
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

    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(factory))
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
            return True, _image()

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

    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(factory))
    with src.open_source("stream:http://cam/v.mjpg"):
        pass
    assert [len(c) for c in calls] == [3, 2]


def test_secret_scope_failure_is_source_error(monkeypatch: pytest.MonkeyPatch,
                                              fake_cv2: dict[str, Any]) -> None:
    import agent.secret_scope as scope

    def raising(name: str, default: Any = None) -> Any:
        raise RuntimeError("multiplex scope missing")

    monkeypatch.setattr(scope, "get_secret", raising)
    with pytest.raises(SourceError, match="RuntimeError"):
        src.open_source("rtsp://u:${X}@cam/live").open()


# ----------------------------------------------------------------------------- snapshot


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict[str, str] | None = None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def open(self, request: Any, timeout: float) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _snapshot(opener: FakeOpener, spec: Any = "https://cam.local/snap.jpg", **kw: Any):
    parsed = SourceSpec.parse(spec)
    return src.SnapshotUrlSource(parsed, opener=opener, **kw)


def test_snapshot_reads_and_decodes(shim_binding) -> None:
    opener = FakeOpener([FakeResponse(_png_bytes(_image(color=(9, 8, 7))))])
    snap = _snapshot(opener, {"kind": "snapshot", "url": "https://cam.local/s.jpg",
                              "timeout": 4})
    snap.open()
    ts, frame = snap.read()
    assert tuple(frame[0, 0]) == (9, 8, 7)
    assert opener.timeouts == [4.0]
    req = opener.requests[0]
    assert req.get_header("User-agent").startswith("hermes-sentinel")
    assert req.get_header("Authorization") is None
    snap.close()
    with pytest.raises(SourceError, match="not open"):
        snap.read()


def test_snapshot_basic_auth_from_secrets(shim_binding) -> None:
    shim_binding.secrets.update({"CAM_USER": "alice", "CAM_PASS": "s3cret"})
    opener = FakeOpener([FakeResponse(_png_bytes(_image()))])
    snap = _snapshot(opener, {"kind": "snapshot", "url": "https://cam/s.jpg",
                              "username_secret": "CAM_USER", "password_secret": "CAM_PASS"})
    with snap:
        snap.read()
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert opener.requests[0].get_header("Authorization") == expected
    assert "s3cret" not in repr(snap)


def test_snapshot_bearer_and_placeholder(shim_binding) -> None:
    shim_binding.secrets.update({"TOK": "abc", "KEY": "k&y"})
    opener = FakeOpener([FakeResponse(_png_bytes(_image()))])
    snap = _snapshot(opener, {"kind": "snapshot", "url": "https://cam/s.jpg?key=${KEY}",
                              "token_secret": "TOK"})
    with snap:
        snap.read()
    req = opener.requests[0]
    assert req.get_header("Authorization") == "Bearer abc"
    assert req.full_url == "https://cam/s.jpg?key=k%26y"


def test_snapshot_missing_auth_secret(shim_binding) -> None:
    snap = _snapshot(FakeOpener([]), {"kind": "snapshot", "url": "https://cam/s.jpg",
                                      "token_secret": "MISSING_TOKEN"})
    with pytest.raises(SourceError, match="MISSING_TOKEN"):
        snap.open()
    assert not snap.is_open


@pytest.mark.parametrize(
    ("outcome", "exc_type", "match"),
    [
        (urllib.error.HTTPError("u", 401, "no", {}, None), SourceError, "401"),
        (urllib.error.HTTPError("u", 503, "busy", {}, None), SourceDisconnected, "503"),
        (urllib.error.URLError("refused"), SourceDisconnected, "unreachable"),
        (TimeoutError("slow"), SourceDisconnected, "timed out"),
        (ConnectionResetError("reset"), SourceDisconnected, "network error"),
        (FakeResponse(b"x", status=304), SourceDisconnected, "HTTP 304"),
        (FakeResponse(b"garbage"), SourceError, "decodable"),
        (FakeResponse(b"x" * 50), SourceError, "larger"),
        (FakeResponse(b"x", headers={"Content-Length": "999"}), SourceError, "larger"),
    ],
)
def test_snapshot_errors(outcome: Any, exc_type: type, match: str) -> None:
    snap = _snapshot(FakeOpener([outcome]), max_bytes=40)
    snap.open()
    with pytest.raises(exc_type, match=match) as exc:
        snap.read()
    if exc_type is SourceError and "401" in match:
        assert not isinstance(exc.value, SourceDisconnected)
    assert snap.is_open  # a failed poll does not close a snapshot source


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    seen_auth: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802
        _Handler.seen_auth.append(self.headers.get("Authorization"))
        if self.path.startswith("/redirect-other"):
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/img")
            self.end_headers()
            return
        if self.path.startswith("/redirect-same"):
            self.send_response(302)
            self.send_header("Location", "/img")
            self.end_headers()
            return
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_Handler.payload)))
        self.end_headers()
        self.wfile.write(_Handler.payload)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def http_server():
    _Handler.payload = _png_bytes(_image(color=(4, 5, 6)))
    _Handler.seen_auth = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_snapshot_real_http(http_server, shim_binding) -> None:
    shim_binding.secrets["TOK"] = "abc"
    port = http_server.server_address[1]
    snap = src.open_source({"kind": "snapshot", "url": f"http://127.0.0.1:{port}/img",
                            "token_secret": "TOK"})
    with snap:
        _, frame = snap.read()
    assert tuple(frame[0, 0]) == (4, 5, 6)
    assert _Handler.seen_auth == ["Bearer abc"]


def test_snapshot_redirect_keeps_auth_same_host(http_server, shim_binding) -> None:
    shim_binding.secrets["TOK"] = "abc"
    port = http_server.server_address[1]
    snap = src.open_source({"kind": "snapshot", "url": f"http://127.0.0.1:{port}/redirect-same",
                            "token_secret": "TOK"})
    with snap:
        snap.read()
    assert _Handler.seen_auth == ["Bearer abc", "Bearer abc"]


def test_snapshot_redirect_drops_auth_cross_host(http_server, shim_binding) -> None:
    shim_binding.secrets["TOK"] = "abc"
    port = http_server.server_address[1]
    snap = src.open_source({"kind": "snapshot",
                            "url": f"http://127.0.0.1:{port}/redirect-other",
                            "token_secret": "TOK"})
    with snap:
        _, frame = snap.read()
    assert tuple(frame[0, 0]) == (4, 5, 6)
    assert _Handler.seen_auth == ["Bearer abc", None]


def test_snapshot_real_timeout(http_server) -> None:
    port = http_server.server_address[1]
    snap = src.open_source({"kind": "snapshot", "url": f"http://127.0.0.1:{port}/slow",
                            "timeout": 0.2})
    snap.open()
    start = time.monotonic()
    with pytest.raises(SourceDisconnected):
        snap.read()
    assert time.monotonic() - start < 1.0


def test_snapshot_unreachable_real() -> None:
    snap = src.open_source({"kind": "snapshot", "url": "http://127.0.0.1:9/x.jpg",
                            "timeout": 1})
    snap.open()
    with pytest.raises(SourceDisconnected):
        snap.read()


# ----------------------------------------------------------------------------- screen


class FakeMSS:
    created: list[FakeMSS] = []
    monitors = [
        {"left": 0, "top": 0, "width": 300, "height": 100},
        {"left": 0, "top": 0, "width": 200, "height": 100},
        {"left": 200, "top": 0, "width": 100, "height": 80},
    ]

    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.grabs: list[dict[str, int]] = []
        self.closed = False
        self.fail = False
        FakeMSS.created.append(self)

    def grab(self, box: dict[str, int]) -> np.ndarray:
        if self.fail:
            raise RuntimeError("XGetImage failed")
        self.grabs.append(dict(box))
        img = np.zeros((box["height"], box["width"], 4), dtype=np.uint8)
        img[..., 0] = 11
        img[..., 3] = 255
        return img

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_mss(monkeypatch: pytest.MonkeyPatch):
    FakeMSS.created = []
    monkeypatch.setattr(src, "_mss_factory", lambda purpose: (FakeMSS, RuntimeError))
    yield FakeMSS
    FakeMSS.created = []


def test_screen_whole_monitor(fake_mss) -> None:
    screen = src.open_source("screen:2", clock=lambda: 7.0)
    assert isinstance(screen, src.ScreenSource)
    screen.open()
    ts, frame = screen.read()
    assert ts == 7.0 and frame.shape == (80, 100, 3) and frame[0, 0, 0] == 11
    assert fake_mss.created[0].grabs == [{"left": 200, "top": 0, "width": 100, "height": 80}]
    screen.close()
    assert fake_mss.created[0].closed
    screen.close()


def test_screen_region_clipped(fake_mss) -> None:
    with src.open_source("screen:2@50,10,100,100") as screen:
        _, frame = screen.read()
    assert fake_mss.created[0].grabs == [{"left": 250, "top": 10, "width": 50, "height": 70}]
    assert frame.shape == (70, 50, 3)


def test_screen_region_outside(fake_mss) -> None:
    screen = src.open_source("screen:1@500,500,10,10")
    with pytest.raises(SourceSpecError, match="outside"):
        screen.open()
    assert not screen.is_open and fake_mss.created[0].closed


def test_screen_missing_monitor(fake_mss) -> None:
    with pytest.raises(SourceDisconnected, match="monitor 5 not found"):
        src.open_source("screen:5").open()


def test_screen_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory() -> Any:
        raise RuntimeError("$DISPLAY not set")

    monkeypatch.setattr(src, "_mss_factory", lambda p: (factory, RuntimeError))
    with pytest.raises(SourceDisconnected, match="graphical session"):
        src.open_source("screen").open()


def test_screen_thread_affinity(fake_mss) -> None:
    screen = src.open_source("screen:1")
    screen.open()
    screen.read()
    result: list[Any] = []
    t = threading.Thread(target=lambda: result.append(screen.read()))
    t.start()
    t.join(2)
    assert result
    assert len(fake_mss.created) == 2
    assert fake_mss.created[0].closed
    assert fake_mss.created[1].thread != fake_mss.created[0].thread
    screen.close()
    assert fake_mss.created[1].closed


def test_screen_grab_failure(fake_mss) -> None:
    screen = src.open_source("screen:1")
    screen.open()
    fake_mss.created[0].fail = True
    with pytest.raises(SourceDisconnected, match="grab failed"):
        screen.read()
    # The handle is recreated on the next read.
    _, frame = screen.read()
    assert frame.shape == (100, 200, 3)
    screen.close()


def test_real_mss_factory_resolves() -> None:
    factory, err = src._mss_factory("test")
    assert callable(factory)
    assert issubclass(err, BaseException)


# ----------------------------------------------------------------------------- folder


def _write(path: Path, img: np.ndarray, mtime: float) -> None:
    assert cv2.imwrite(str(path), img)
    os.utime(path, (mtime, mtime))


def test_folder_newest_image(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "a.png", _image(color=(1, 1, 1)), now - 30)
    _write(tmp_path / "b.jpg", _image(color=(200, 200, 200)), now - 10)
    _write(tmp_path / "c.png", _image(color=(3, 3, 3)), now - 20)
    (tmp_path / "notes.txt").write_text("not an image")
    os.utime(tmp_path / "notes.txt", (now, now))
    _write(tmp_path / ".hidden.png", _image(color=(9, 9, 9)), now)
    (tmp_path / "subdir.png").mkdir()

    folder = src.open_source(f"folder:{tmp_path}", clock=lambda: 42.0)
    assert isinstance(folder, src.FolderSource)
    folder.open()
    ts, frame = folder.read()
    assert ts == 42.0
    assert frame.shape == (48, 64, 3)
    assert abs(int(frame[0, 0, 0]) - 200) <= 3  # JPEG is lossy
    _write(tmp_path / "d.png", _image(color=(50, 60, 70)), now + 5)
    _, frame = folder.read()
    assert tuple(frame[0, 0]) == (50, 60, 70)
    folder.close()


def test_folder_caches_decoded_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "a.png", _image(), time.time())
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)))
    folder.open()
    calls: list[str] = []
    real = src._decode_image
    monkeypatch.setattr(src, "_decode_image",
                        lambda data, origin: calls.append(origin) or real(data, origin=origin))
    _, f1 = folder.read()
    _, f2 = folder.read()
    assert len(calls) == 1
    f1[:] = 0  # callers get copies
    _, f3 = folder.read()
    assert np.array_equal(f2, f3) and f3.any()


def test_folder_partial_newest_falls_back(tmp_path: Path) -> None:
    now = time.time()
    _write(tmp_path / "good.png", _image(color=(5, 5, 5)), now - 10)
    folder = src.open_source({"kind": "folder", "path": str(tmp_path)})
    folder.open()
    folder.read()
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0 truncated")
    os.utime(broken, (now, now))
    _, frame = folder.read()
    assert tuple(frame[0, 0]) == (5, 5, 5)


def test_folder_only_broken_is_not_ready(tmp_path: Path) -> None:
    (tmp_path / "x.jpg").write_bytes(b"junk")
    with src.open_source(f"folder:{tmp_path}") as folder, pytest.raises(SourceNotReady):
        folder.read()


def test_folder_empty_and_missing(tmp_path: Path) -> None:
    folder = src.open_source(f"folder:{tmp_path}")
    folder.open()
    with pytest.raises(SourceNotReady, match="no images"):
        folder.read()
    folder.close()
    missing = src.open_source(f"folder:{tmp_path / 'nope'}")
    with pytest.raises(SourceDisconnected, match="not a directory"):
        missing.open()
    gone = tmp_path / "gone"
    gone.mkdir()
    g = src.open_source(f"folder:{gone}")
    g.open()
    gone.rmdir()
    with pytest.raises(SourceDisconnected, match="no longer exists"):
        g.read()


def test_folder_size_cap(tmp_path: Path) -> None:
    _write(tmp_path / "big.png", _image(), time.time())
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)), max_file_bytes=10)
    folder.open()
    with pytest.raises(SourceNotReady):
        folder.read()


def test_folder_tie_break_by_name(tmp_path: Path) -> None:
    t = time.time()
    _write(tmp_path / "a.png", _image(color=(1, 1, 1)), t)
    _write(tmp_path / "b.png", _image(color=(2, 2, 2)), t)
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)))
    folder.open()
    found = folder.newest()
    assert found is not None and found[0].name == "b.png"


def test_folder_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "pics").mkdir()
    folder = src.FolderSource(SourceSpec(kind="folder", target="~/pics"))
    assert folder.path == tmp_path / "pics"


def test_folder_concurrent_reads(tmp_path: Path) -> None:
    _write(tmp_path / "a.png", _image(color=(7, 7, 7)), time.time())
    folder = src.open_source(f"folder:{tmp_path}")
    folder.open()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                _, frame = folder.read()
                assert tuple(frame[0, 0]) == (7, 7, 7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors


# ----------------------------------------------------------------------------- factory


def test_open_source_auto_open(tmp_path: Path) -> None:
    _write(tmp_path / "a.png", _image(), time.time())
    folder = src.open_source(str(tmp_path), auto_open=True)
    assert folder.is_open
    folder.close()


def test_open_source_named_stream(fake_cv2: dict[str, Any]) -> None:
    s = src.open_source("door", named_streams={"door": "rtsp://cam/live"})
    assert isinstance(s, src.StreamSource) and s.name == "door"


def test_open_source_bad_spec() -> None:
    with pytest.raises(SourceSpecError):
        src.open_source("???")


# ----------------------------------------------------------------------------- probe


@pytest.fixture
def probe_cleanup():
    """Make sure no stuck probe thread leaks into another test."""
    gates: list[threading.Event] = []
    yield gates
    for g in gates:
        g.set()
    thread = src._probe_thread
    if thread is not None:
        thread.join(5)


def test_list_sources(monkeypatch: pytest.MonkeyPatch, fake_mss, probe_cleanup) -> None:
    def factory(index: Any, *a: Any) -> FakeCapture:
        return FakeCapture(index, opened=index in (0, 2))

    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(factory))
    infos = src.list_sources(
        named_streams={"door": "rtsp://u:${PW}@cam/live", "porch": "http://cam/s.jpg",
                       "broken": "ftp://x"},
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


def test_list_sources_never_hangs(monkeypatch: pytest.MonkeyPatch, fake_mss,
                                  probe_cleanup) -> None:
    gate = threading.Event()
    probe_cleanup.append(gate)

    def factory(index: Any, *a: Any) -> FakeCapture:
        if index == 1:
            gate.wait(10)
        return FakeCapture(index, opened=True)

    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(factory))
    start = time.monotonic()
    infos = src.list_sources(max_camera_index=2, total_timeout=0.5, per_camera_timeout=0.2,
                             probe_screens=False)
    assert time.monotonic() - start < 2
    by_spec = {i.spec: i for i in infos}
    assert by_spec["camera:0"].available is True
    assert by_spec["camera:1"].available is None and "timed out" in by_spec["camera:1"].detail
    # A second probe while the first is stuck does not start another thread.
    again = src.list_sources(max_camera_index=1, total_timeout=0.3, probe_screens=False)
    assert all("still running" in i.detail for i in again)
    gate.set()
    src._probe_thread.join(5)
    assert not src._probe_thread.is_alive()


def test_list_sources_camera_error(monkeypatch: pytest.MonkeyPatch, probe_cleanup) -> None:
    def factory(index: Any, *a: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(factory))
    infos = src.list_sources(max_camera_index=0, probe_screens=False)
    assert infos[0].available is False and "boom" in infos[0].detail


def test_list_sources_screen_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_display() -> Any:
        raise RuntimeError("no display")

    monkeypatch.setattr(src, "_mss_factory", lambda p: (no_display, RuntimeError))
    infos = src.list_sources(probe_cameras=False)
    assert infos[0].available is False and "no display" in infos[0].detail

    gate = threading.Event()

    class SlowMSS(FakeMSS):
        def __init__(self) -> None:
            gate.wait(5)
            super().__init__()

    monkeypatch.setattr(src, "_mss_factory", lambda p: (SlowMSS, RuntimeError))
    start = time.monotonic()
    infos = src.list_sources(probe_cameras=False, total_timeout=0.3)
    gate.set()
    assert time.monotonic() - start < 2
    assert infos[0].available is None and "timed out" in infos[0].detail


def test_list_sources_clamps_camera_range(monkeypatch: pytest.MonkeyPatch, probe_cleanup) -> None:
    monkeypatch.setattr(src, "_cv2", lambda p: make_fake_cv2(
        lambda i, *a: FakeCapture(i, opened=False)))
    infos = src.list_sources(max_camera_index=-5, probe_screens=False)
    assert [i.spec for i in infos] == ["camera:0"]
