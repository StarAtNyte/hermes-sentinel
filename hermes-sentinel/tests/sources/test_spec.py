"""Source spec parsing, validation and URL redaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from harness.plugin_loader import plugin_module

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
    spec = SourceSpec.parse(
        {"kind": "screen", "monitor": "2", "region": "0,0,100,50", "name": "chart"}
    )
    assert spec == SourceSpec(kind="screen", target=2, region=(0, 0, 100, 50), name="chart")
    spec = SourceSpec.parse({"spec": "camera:1", "width": 1280, "height": 720, "timeout": 3})
    assert (spec.target, spec.width, spec.height, spec.timeout) == (1, 1280, 720, 3.0)
    spec = SourceSpec.parse(
        {
            "kind": "snapshot",
            "url": "https://cam/s.jpg",
            "username_secret": "CAM_USER",
            "password_secret": "CAM_PASS",
        }
    )
    assert spec.username_secret == "CAM_USER"
    spec = SourceSpec.parse(
        {"kind": "stream", "target": "door"}, named_streams={"door": "rtsp://cam/live"}
    )
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
        {
            "kind": "snapshot",
            "target": "http://c/s",
            "token_secret": "T",
            "username_secret": "U",
            "password_secret": "P",
        },
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
    spec = SourceSpec.parse(
        {"kind": "snapshot", "url": "https://u:${P}@cam/s.jpg", "token_secret": "CAM_TOKEN"}
    )
    d = spec.to_dict()
    assert d["kind"] == "snapshot" and d["token_secret"] == "CAM_TOKEN"
    assert d["spec"].startswith("snapshot:https://u:${P}@cam")
    assert SourceSpec(kind="screen", target=1, region=(0, 0, 5, 5)).to_dict()["region"] == [
        0,
        0,
        5,
        5,
    ]


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
