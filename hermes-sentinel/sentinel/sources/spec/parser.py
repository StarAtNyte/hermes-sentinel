"""Pure parsing of spec strings, indexes and mappings into :class:`SourceSpec`."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any

from ..constants import HTTP_SCHEMES, VIDEO_EXTENSIONS, SourceKind
from ..errors import SourceSpecError
from .model import SourceSpec
from .urls import looks_like_stream, redact_url

_REGION_RE = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")
_TARGET_ALIASES = ("url", "path", "index", "monitor")
_OVERRIDE_KEYS = (
    "name",
    "timeout",
    "width",
    "height",
    "username_secret",
    "password_secret",
    "token_secret",
    "loop",
    "speed",
)
_FILE_OPTION_KEYS = frozenset({"loop", "speed"})
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def parse_index(text: Any, kind: str) -> int:
    """Parse a camera or monitor index, raising :class:`SourceSpecError` if not an integer."""
    try:
        return int(str(text).strip())
    except ValueError as exc:
        raise SourceSpecError(f"{kind} index must be an integer, got {text!r}") from exc


def parse_region(text: str) -> tuple[int, int, int, int]:
    """Parse ``left,top,width,height``."""
    match = _REGION_RE.match(text or "")
    if not match:
        raise SourceSpecError("region must be 'left,top,width,height'")
    left, top, width, height = (int(g) for g in match.groups())
    return left, top, width, height


def _parse_bool(text: str, key: str) -> bool:
    word = text.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise SourceSpecError(f"{key} must be true or false, got {text!r}")


def parse_file_spec(rest: str) -> SourceSpec:
    """Parse the part after ``file:`` (or a bare video path): ``/path.mp4[?loop=true&speed=N]``.

    The ``?options`` tail is only taken as options when every key is ``loop`` or ``speed``;
    otherwise the whole text is the path.
    """
    path, _, query = rest.strip().rpartition("?")
    pairs = [part.partition("=") for part in query.split("&")] if path else []
    if not path or not all(key in _FILE_OPTION_KEYS for key, _, _ in pairs):
        return SourceSpec(kind="file", target=rest.strip())
    options: dict[str, Any] = {}
    for key, _, value in pairs:
        options[key] = _parse_bool(value, key) if key == "loop" else parse_index(value, key)
    return SourceSpec(kind="file", target=path, **options)


def _file_target(rest: str) -> str:
    """Drop the ``//`` of a ``file:///abs/path`` URL form."""
    if rest.startswith("///"):
        return rest[2:]
    return rest


def _is_video_path(raw: str) -> bool:
    return os.path.splitext(raw.split("?", 1)[0])[1].lower() in VIDEO_EXTENSIONS


def named_stream(name: str, url: str) -> SourceSpec:
    """Build the spec for a configured ``stream_urls`` entry."""
    if not isinstance(url, str):
        raise SourceSpecError(f"configured stream {name!r} must map to a URL string")
    kind: SourceKind = "stream"
    target = url.strip()
    lowered = target.lower()
    if lowered.startswith("snapshot:"):
        kind, target = "snapshot", target.split(":", 1)[1].strip()
    elif lowered.startswith("stream:"):
        target = target.split(":", 1)[1].strip()
    elif lowered.split(":", 1)[0] in HTTP_SCHEMES and not looks_like_stream(target):
        kind = "snapshot"
    return SourceSpec(kind=kind, target=target, name=name)


def _parse_prefixed(raw: str, named: Mapping[str, str]) -> SourceSpec | None:
    """Handle ``prefix:rest`` forms (camera:, screen:, folder:, snapshot:, stream:, URLs)."""
    prefix, sep, rest = raw.partition(":")
    if not sep:
        return None
    prefix_l = prefix.lower()
    if prefix_l in ("camera", "webcam", "cam"):
        return SourceSpec(kind="camera", target=parse_index(rest, "camera"))
    if prefix_l in ("screen", "monitor"):
        index_text, at, region_text = rest.partition("@")
        index = parse_index(index_text, "screen") if index_text.strip() else 1
        region = parse_region(region_text) if at else None
        return SourceSpec(kind="screen", target=index, region=region)
    if prefix_l in ("folder", "dir"):
        return SourceSpec(kind="folder", target=rest.strip())
    if prefix_l == "file":
        return parse_file_spec(_file_target(rest))
    if prefix_l == "snapshot":
        return SourceSpec(kind="snapshot", target=rest.strip())
    if prefix_l == "stream":
        target = rest.strip()
        if target in named:
            return named_stream(target, named[target])
        return SourceSpec(kind="stream", target=target)
    if rest.startswith("//"):
        if prefix_l in HTTP_SCHEMES and not looks_like_stream(raw):
            return SourceSpec(kind="snapshot", target=raw)
        return SourceSpec(kind="stream", target=raw)
    return None


def parse_string(text: str, *, named_streams: Mapping[str, str] | None) -> SourceSpec:
    """Parse a spec string such as ``camera:0``, ``screen:1@0,0,640,480`` or a URL."""
    raw = text.strip()
    if not raw:
        raise SourceSpecError("empty source spec")
    named = dict(named_streams or {})
    lowered = raw.lower()
    if raw.isdigit():
        return SourceSpec(kind="camera", target=int(raw))
    if raw in named:
        return named_stream(raw, named[raw])
    if lowered in ("camera", "webcam"):
        return SourceSpec(kind="camera", target=0)
    if lowered == "screen":
        return SourceSpec(kind="screen", target=1)
    parsed = _parse_prefixed(raw, named)
    if parsed is not None:
        return parsed
    if os.path.isdir(os.path.expanduser(raw)):
        return SourceSpec(kind="folder", target=raw)
    if _is_video_path(raw) and "://" not in raw:
        return parse_file_spec(raw)
    raise SourceSpecError(
        f"unrecognised source {redact_url(raw)!r}; use camera:N, screen[:N], a URL, "
        "folder:/path, file:/path/video.mp4, or a configured stream name"
    )


def _base_from_mapping(
    data: Mapping[str, Any], named_streams: Mapping[str, str] | None
) -> SourceSpec:
    if "spec" in data:
        return parse_string(str(data["spec"]), named_streams=named_streams)
    kind = data.get("kind")
    target = data.get("target")
    for alias in _TARGET_ALIASES:
        if target is None and alias in data:
            target = data[alias]
    if kind is None or target is None:
        raise SourceSpecError("source mapping needs 'kind' and 'target' (or 'spec')")
    if kind in ("camera", "screen") and isinstance(target, str):
        target = parse_index(target, kind)
    if kind == "stream" and isinstance(target, str) and named_streams:
        target = named_streams.get(target, target)
    return SourceSpec(kind=kind, target=target)


def _region_override(region: Any) -> tuple[int, ...]:
    if isinstance(region, str):
        region = parse_region(region)
    try:
        return tuple(int(v) for v in region)
    except (TypeError, ValueError) as exc:
        raise SourceSpecError("region must be four integers") from exc


def parse_mapping(
    data: Mapping[str, Any], *, named_streams: Mapping[str, str] | None
) -> SourceSpec:
    """Parse a mapping with ``spec`` or ``kind``/``target`` plus optional overrides."""
    allowed = {f.name for f in fields(SourceSpec)} | {"spec", *_TARGET_ALIASES}
    unknown = set(data) - allowed
    if unknown:
        raise SourceSpecError(f"unknown source fields: {', '.join(sorted(unknown))}")
    base = _base_from_mapping(data, named_streams)
    overrides: dict[str, Any] = {k: data[k] for k in _OVERRIDE_KEYS if data.get(k) is not None}
    if data.get("region") is not None:
        overrides["region"] = _region_override(data["region"])
    return replace(base, **overrides) if overrides else base


def parse_spec(
    value: SourceSpec | str | int | Mapping[str, Any],
    *,
    named_streams: Mapping[str, str] | None = None,
) -> SourceSpec:
    """Build a spec from a spec string, camera index, mapping, or existing spec."""
    if isinstance(value, SourceSpec):
        return value
    if isinstance(value, bool):
        raise SourceSpecError("source spec must be a string, index or mapping")
    if isinstance(value, int):
        return SourceSpec(kind="camera", target=value)
    if isinstance(value, Mapping):
        return parse_mapping(value, named_streams=named_streams)
    if not isinstance(value, str):
        raise SourceSpecError("source spec must be a string, index or mapping")
    return parse_string(value, named_streams=named_streams)
