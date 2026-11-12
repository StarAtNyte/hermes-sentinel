"""Snapshot URL source, including real loopback HTTP."""

from __future__ import annotations

import base64
import dataclasses
import http.server
import io
import threading
import time
import urllib.error
from typing import Any

import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import image, png_bytes

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


def _configured(spec: Any, **kw: Any):
    """Open ``spec`` as if it came from the user's stream_urls config."""
    return src.open_source(dataclasses.replace(SourceSpec.parse(spec), configured=True), **kw)


def _snapshot(opener: FakeOpener, spec: Any = "https://cam.local/snap.jpg", **kw: Any):
    parsed = dataclasses.replace(SourceSpec.parse(spec), configured=True)  # as from stream_urls
    return src.SnapshotUrlSource(parsed, opener=opener, **kw)


def test_snapshot_reads_and_decodes(shim_binding) -> None:
    opener = FakeOpener([FakeResponse(png_bytes(image(color=(9, 8, 7))))])
    snap = _snapshot(opener, {"kind": "snapshot", "url": "https://cam.local/s.jpg", "timeout": 4})
    snap.open()
    ts, frame = snap.read()
    assert tuple(frame[0, 0]) == (9, 8, 7)
    assert opener.timeouts == [4.0]
    req = opener.requests[0]
    assert req.get_header("User-agent").startswith("sentinel-vision")
    assert req.get_header("Authorization") is None
    snap.close()
    with pytest.raises(SourceError, match="not open"):
        snap.read()


def test_snapshot_basic_auth_from_secrets(shim_binding) -> None:
    shim_binding.secrets.update({"CAM_USER": "alice", "CAM_PASS": "s3cret"})
    opener = FakeOpener([FakeResponse(png_bytes(image()))])
    snap = _snapshot(
        opener,
        {
            "kind": "snapshot",
            "url": "https://cam/s.jpg",
            "username_secret": "CAM_USER",
            "password_secret": "CAM_PASS",
        },
    )
    with snap:
        snap.read()
    expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert opener.requests[0].get_header("Authorization") == expected
    assert "s3cret" not in repr(snap)


def test_snapshot_bearer_and_placeholder(shim_binding) -> None:
    shim_binding.secrets.update({"TOK": "abc", "KEY": "k&y"})
    opener = FakeOpener([FakeResponse(png_bytes(image()))])
    snap = _snapshot(
        opener, {"kind": "snapshot", "url": "https://cam/s.jpg?key=${KEY}", "token_secret": "TOK"}
    )
    with snap:
        snap.read()
    req = opener.requests[0]
    assert req.get_header("Authorization") == "Bearer abc"
    assert req.full_url == "https://cam/s.jpg?key=k%26y"


def test_snapshot_missing_auth_secret(shim_binding) -> None:
    snap = _snapshot(
        FakeOpener([]),
        {"kind": "snapshot", "url": "https://cam/s.jpg", "token_secret": "MISSING_TOKEN"},
    )
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
    _Handler.payload = png_bytes(image(color=(4, 5, 6)))
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
    snap = _configured(
        {"kind": "snapshot", "url": f"http://127.0.0.1:{port}/img", "token_secret": "TOK"}
    )
    with snap:
        _, frame = snap.read()
    assert tuple(frame[0, 0]) == (4, 5, 6)
    assert _Handler.seen_auth == ["Bearer abc"]


def test_snapshot_redirect_keeps_auth_same_host(http_server, shim_binding) -> None:
    shim_binding.secrets["TOK"] = "abc"
    port = http_server.server_address[1]
    snap = _configured(
        {"kind": "snapshot", "url": f"http://127.0.0.1:{port}/redirect-same", "token_secret": "TOK"}
    )
    with snap:
        snap.read()
    assert _Handler.seen_auth == ["Bearer abc", "Bearer abc"]


def test_snapshot_redirect_drops_auth_cross_host(http_server, shim_binding) -> None:
    shim_binding.secrets["TOK"] = "abc"
    port = http_server.server_address[1]
    snap = _configured(
        {
            "kind": "snapshot",
            "url": f"http://127.0.0.1:{port}/redirect-other",
            "token_secret": "TOK",
        }
    )
    with snap:
        _, frame = snap.read()
    assert tuple(frame[0, 0]) == (4, 5, 6)
    assert _Handler.seen_auth == ["Bearer abc", None]


def test_snapshot_real_timeout(http_server) -> None:
    port = http_server.server_address[1]
    snap = _configured({"kind": "snapshot", "url": f"http://127.0.0.1:{port}/slow", "timeout": 0.2})
    snap.open()
    start = time.monotonic()
    with pytest.raises(SourceDisconnected):
        snap.read()
    assert time.monotonic() - start < 1.0


def test_snapshot_unreachable_real() -> None:
    snap = _configured({"kind": "snapshot", "url": "http://127.0.0.1:9/x.jpg", "timeout": 1})
    snap.open()
    with pytest.raises(SourceDisconnected):
        snap.read()
