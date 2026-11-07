"""Make source specs and error strings safe to return: no credentials, bounded length."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import access
from .http import MAX_SOURCE

__all__ = ["mask", "source_key", "source_label_kind"]

_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*:)(?=[A-Za-z][A-Za-z0-9+.-]*://)")
_GENERIC_SEGMENT_RE = re.compile(
    r"^(?:stream|ch|channel|cam|camera|video|profile|live|main|sub|h26[45]|streaming|channels|"
    r"media|unicast|onvif)\d*$|^\d{1,4}$",
    re.IGNORECASE,
)
_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/?#@]*)@")


def _mask_rtsp_path(word: str) -> str:
    """Hide non-generic RTSP path segments (``rtsp://h/live/<token>``) behind a short digest.

    The digest keeps two cameras on one recorder distinguishable without revealing the path.
    """
    low = word.lower()
    if not low.startswith(("rtsp://", "rtsps://")) or "${" in word:
        return word
    scheme, _, rest = word.partition("://")
    hostpart, slash, tail = rest.partition("/")
    path = tail.split("?", 1)[0]
    segments = [seg for seg in path.split("/") if seg]
    if slash and segments and not all(_GENERIC_SEGMENT_RE.match(seg) for seg in segments):
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:4]
        query = f"?{tail.split('?', 1)[1]}" if "?" in tail else ""
        return f"{scheme}://{hostpart}/***~{digest}{query}"
    return word


def _mask_word(redact: Any, word: str) -> str:
    """Redact one ``scheme://`` word, including ``snapshot:https://...`` style prefixes."""
    prefix = ""
    m = _PREFIX_RE.match(word)
    if m:
        prefix, word = m.group(1), word[m.end() :]
    word = redact(word)
    m = _USERINFO_RE.match(word)
    if m and "${" not in m.group(2):
        word = f"{m.group(1)}***@{word[m.end() :]}"  # token-only userinfo
    return prefix + _mask_rtsp_path(word)


def mask(text: Any) -> str:
    """Make a source spec / error string safe to return: no URL credentials, bounded."""
    redact = access.module("sources").redact_url
    out = " ".join(_mask_word(redact, w) if "://" in w else w for w in str(text or "").split(" "))
    return out[:MAX_SOURCE]


def source_key(live: Any, spec: str) -> str:
    """The masked grouping key of a source spec, as the engine would key it."""
    engine = live.engine_if_built
    try:
        key = (
            engine.source_key(spec)
            if engine is not None
            else access.module("engine").default_source_key(spec, live.config.stream_urls)
        )
    except Exception:  # noqa: BLE001
        key = str(spec).strip()
    return mask(key)


def source_label_kind(spec: str, named: Any) -> tuple[str, str]:
    """``(display label, kind)`` of a spec, ``(masked spec, "unknown")`` if it will not parse."""
    sources = access.module("sources")
    try:
        parsed = sources.SourceSpec.parse(spec, named_streams=named)
        return mask(parsed.display_name), str(parsed.kind)
    except Exception:  # noqa: BLE001
        return mask(spec), "unknown"
