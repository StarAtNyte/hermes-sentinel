"""Frame sources: webcams, RTSP/HTTP streams, snapshot URLs, the screen, image folders.

Every source, real or fake, follows the ``FrameSource`` protocol below:

    src = open_source("camera:0")
    src.open()
    ts, frame = src.read()   # frame is an HxWx3 uint8 BGR ndarray, ts is wall-clock seconds
    src.close()

``read()`` raises ``SourceError`` (or a subclass) when the source is not open, has
disconnected, or has nothing to give. Sources are thread-safe: ``open``/``read``/``close``
serialise on a per-source lock, so ``close()`` from another thread waits for an in-flight read.

Spec strings understood by :meth:`SourceSpec.parse` / :func:`open_source`:

=====================================  ==========================================================
``camera:0`` or ``0``                  local camera by index (OpenCV)
``rtsp://host/path``                   RTSP stream (OpenCV/FFmpeg)
``stream:http://host/video.mjpg``      HTTP(S) MJPEG / video stream (OpenCV/FFmpeg)
``http(s)://host/snap.jpg``            HTTP(S) single-JPEG endpoint polled per read (urllib);
                                       URLs whose path looks like a video stream (``.mjpg``,
                                       ``.m3u8``, ...) are treated as streams instead
``snapshot:https://host/snap.jpg``     force snapshot mode
``screen`` / ``screen:2``              whole monitor (1 = primary, 0 = all monitors)
``screen:1@100,200,640,480``           region ``left,top,width,height`` relative to the monitor
``folder:/path/to/dir`` or a dir path  newest image file in a directory
``<name>`` / ``stream:<name>``         a configured stream (``stream_urls`` config)
=====================================  ==========================================================

Credentials never live in specs or config. A URL may reference secrets as ``${NAME}``
placeholders (``rtsp://admin:${DOORCAM_PASSWORD}@10.0.0.5/live``); they are resolved through
``agent.secret_scope.get_secret`` at ``open()`` time, never written back, and never shown in
errors or descriptions. A literal password in a URL is rejected. Snapshot sources can also use
HTTP basic auth (``username_secret`` / ``password_secret``) or a bearer token (``token_secret``).

OpenCV and mss are imported lazily, so the plugin loads without them; a source that needs a
missing package raises :class:`MissingDependency` with an install hint.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import importlib
import logging
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:  # numpy is only needed for type hints here.
    import numpy as np

    Frame = np.ndarray
else:
    Frame = object

__all__ = [
    "IMAGE_EXTENSIONS",
    "CameraSource",
    "FolderSource",
    "FrameSource",
    "MissingDependency",
    "ScreenSource",
    "SnapshotUrlSource",
    "SourceDisconnected",
    "SourceError",
    "SourceExhausted",
    "SourceInfo",
    "SourceKind",
    "SourceNotReady",
    "SourceSpec",
    "SourceSpecError",
    "StreamSource",
    "list_sources",
    "open_source",
    "redact_url",
]

log = logging.getLogger(__name__)

SourceKind = Literal["camera", "stream", "snapshot", "screen", "folder"]
SOURCE_KINDS: tuple[str, ...] = ("camera", "stream", "snapshot", "screen", "folder")

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
)
_STREAM_PATH_HINTS = (".mjpg", ".mjpeg", ".m3u8", ".mp4", ".mkv", ".webm", ".flv", ".ts")
_STREAM_SCHEMES = frozenset({"rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "udp", "tcp"})
_HTTP_SCHEMES = frozenset({"http", "https"})

DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 120.0
MAX_CAMERA_INDEX = 64
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_FILE_BYTES = 50 * 1024 * 1024
MAX_FOLDER_ENTRIES = 10_000
_USER_AGENT = "hermes-sentinel/0.1 (+https://github.com/NousResearch/hermes-agent)"

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REGION_RE = re.compile(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")
_SENSITIVE_QUERY_KEYS = frozenset(
    {"password", "passwd", "pwd", "pass", "token", "access_token", "key", "apikey", "api_key",
     "auth", "secret", "sig", "signature"}
)

_INSTALL_HINTS = {
    "cv2": "opencv-python-headless",
    "mss": "mss",
    "numpy": "numpy",
}


# ----------------------------------------------------------------------------- errors


class SourceError(RuntimeError):
    """A frame source cannot produce a frame (closed, disconnected, exhausted)."""


class SourceDisconnected(SourceError):
    """The underlying device or stream went away mid-read."""


class SourceExhausted(SourceError):
    """A finite source (file, fixture replay) has no more frames."""


class SourceNotReady(SourceError):
    """The source is reachable but has nothing to deliver yet (e.g. an empty folder)."""


class SourceSpecError(SourceError, ValueError):
    """A source spec is malformed or unsafe (e.g. a literal password in a URL)."""


class MissingDependency(SourceError, ImportError):
    """An optional runtime package (OpenCV, mss, numpy) is not installed."""

    def __init__(self, module: str, purpose: str) -> None:
        package = _INSTALL_HINTS.get(module, module)
        self.module = module
        self.package = package
        super().__init__(
            f"{purpose} needs the Python package '{package}', which is not installed. It is "
            f"declared in hermes-sentinel's plugin.yaml python_dependencies: reinstall or "
            f"repair the plugin so Hermes installs its dependencies, then restart Hermes."
        )


# ----------------------------------------------------------------------------- protocol


@runtime_checkable
class FrameSource(Protocol):
    """Minimal interface shared by real and fake frame sources."""

    def open(self) -> None:
        """Acquire the device or stream. Must be called before ``read``."""
        ...

    def read(self) -> tuple[float, Frame]:
        """Return ``(timestamp_seconds, frame)`` or raise ``SourceError``."""
        ...

    def close(self) -> None:
        """Release resources. Safe to call more than once."""
        ...


# ----------------------------------------------------------------------------- lazy imports


def _import_optional(module: str, purpose: str) -> ModuleType:
    """Import ``module`` or raise :class:`MissingDependency` naming the pip package."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MissingDependency(module, purpose) from exc


def _cv2(purpose: str) -> ModuleType:
    return _import_optional("cv2", purpose)


def _np(purpose: str) -> ModuleType:
    return _import_optional("numpy", purpose)


def dependency_status() -> dict[str, str | None]:
    """Map each optional runtime module to ``None`` (importable) or an install hint."""
    status: dict[str, str | None] = {}
    for module, package in _INSTALL_HINTS.items():
        try:
            importlib.import_module(module)
            status[module] = None
        except ImportError:
            status[module] = f"missing Python package '{package}'"
    return status


# ----------------------------------------------------------------------------- helpers

T = TypeVar("T")


class _Timeout(Exception):
    """Internal: a call guarded by :func:`_call_with_timeout` did not finish in time."""


def _call_with_timeout(
    fn: Callable[[], T],
    timeout: float,
    *,
    name: str = "sentinel-source-call",
    on_abandoned: Callable[[T], None] | None = None,
) -> T:
    """Run ``fn`` in a daemon thread and wait at most ``timeout`` seconds.

    Raises :class:`_Timeout` if it does not finish. The thread cannot be killed; when it
    eventually returns, ``on_abandoned(result)`` runs so resources (a late ``VideoCapture``)
    are released instead of leaking. Exceptions from ``fn`` propagate to the caller.
    """
    done = threading.Event()
    state: dict[str, Any] = {}
    lock = threading.Lock()

    # Keep the caller's contextvars (e.g. Hermes' secret scope) visible to ``fn``.
    ctx = contextvars.copy_context()

    def runner() -> None:
        try:
            result = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            with lock:
                state["error"] = exc
            done.set()
            return
        with lock:
            abandoned = state.get("abandoned", False)
            state["result"] = result
        done.set()
        if abandoned and on_abandoned is not None:
            try:
                on_abandoned(result)
            except Exception:  # noqa: BLE001 - best-effort cleanup in a detached thread
                log.debug("cleanup after abandoned %s failed", name, exc_info=True)

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    finished = done.wait(max(0.0, timeout))
    with lock:
        if not finished and "result" not in state and "error" not in state:
            state["abandoned"] = True
            raise _Timeout(f"{name} did not finish within {timeout:.1f}s")
    if "error" in state:
        raise state["error"]
    return state["result"]


def _clamp_timeout(value: Any, default: float = DEFAULT_TIMEOUT_S) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout != timeout or timeout <= 0:  # NaN or non-positive
        return default
    return min(timeout, MAX_TIMEOUT_S)


def _as_bgr(frame: Any, *, origin: str) -> Frame:
    """Coerce a decoded image to a contiguous HxWx3 uint8 BGR array or raise SourceError."""
    np = _np("frame handling")
    if frame is None:
        raise SourceError(f"{origin}: no image data")
    arr = np.asarray(frame)
    if arr.size == 0:
        raise SourceError(f"{origin}: empty frame")
    if arr.dtype != np.uint8:
        if arr.dtype == np.uint16:
            arr = (arr >> 8).astype(np.uint8)
        else:
            raise SourceError(f"{origin}: unsupported pixel type {arr.dtype}")
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    elif arr.ndim == 3 and arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    elif arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    elif not (arr.ndim == 3 and arr.shape[2] == 3):
        raise SourceError(f"{origin}: unsupported frame shape {arr.shape}")
    return np.ascontiguousarray(arr)


def _decode_image(data: bytes, *, origin: str) -> Frame:
    """Decode encoded image bytes (JPEG/PNG/...) to BGR."""
    cv2 = _cv2("decoding images")
    np = _np("decoding images")
    if not data:
        raise SourceError(f"{origin}: empty image data")
    buf = np.frombuffer(data, dtype=np.uint8)
    try:
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as exc:  # cv2.error is not a stable public type
        raise SourceError(f"{origin}: cannot decode image ({exc})") from exc
    if img is None:
        raise SourceError(f"{origin}: data is not a decodable image")
    return _as_bgr(img, origin=origin)


def redact_url(url: str) -> str:
    """Return ``url`` with any literal password and sensitive query values replaced by ``***``.

    ``${NAME}`` placeholders are kept (they name a secret, they are not one).
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid url>"
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        user, sep, password = userinfo.partition(":")
        if sep and not _is_placeholder(password):
            password = "***"
        netloc = f"{user}{sep}{password}@{hostport}"
    query = parts.query
    if query:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        redacted = [
            (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS and not _is_placeholder(v) else v)
            for k, v in pairs
        ]
        query = urllib.parse.urlencode(redacted, safe="${}")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _is_placeholder(value: str) -> bool:
    return bool(value) and _PLACEHOLDER_RE.fullmatch(value) is not None


def _get_secret(name: str) -> str | None:
    """Read one secret through Hermes' secret scope (never ``os.environ``)."""
    try:
        from agent.secret_scope import get_secret
    except ImportError as exc:
        raise SourceError(
            f"cannot resolve secret {name!r}: Hermes secret scope is unavailable"
        ) from exc
    try:
        value = get_secret(name)
    except Exception as exc:  # e.g. fail-closed multiplex scope
        raise SourceError(f"cannot resolve secret {name!r}: {type(exc).__name__}") from exc
    if value is None:
        return None
    return str(value)


def _require_secret(name: str, *, origin: str) -> str:
    value = _get_secret(name)
    if not value:
        raise SourceError(
            f"{origin}: secret {name!r} is not set; add it to Hermes' secrets/.env for this profile"
        )
    return value


def _expand_url(template: str, *, origin: str) -> str:
    """Substitute ``${NAME}`` placeholders with URL-quoted secret values."""

    def sub(match: re.Match[str]) -> str:
        return urllib.parse.quote(_require_secret(match.group(1), origin=origin), safe="")

    return _PLACEHOLDER_RE.sub(sub, template)


def _check_url_template(url: str, *, schemes: frozenset[str]) -> None:
    """Validate scheme and reject literal credentials in a URL template."""
    if not isinstance(url, str) or not url.strip():
        raise SourceSpecError("URL must be a non-empty string")
    if any(ch in url for ch in "\r\n\t\x00"):
        raise SourceSpecError("URL contains control characters")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SourceSpecError(f"invalid URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        allowed = ", ".join(sorted(schemes))
        raise SourceSpecError(
            f"unsupported URL scheme {scheme or '(none)'!r}; use one of {allowed}"
        )
    if not parts.hostname and not _PLACEHOLDER_RE.search(parts.netloc):
        raise SourceSpecError("URL has no host")
    if "@" in parts.netloc:
        userinfo = parts.netloc.rpartition("@")[0]
        _, sep, password = userinfo.partition(":")
        if sep and password and not _is_placeholder(password):
            raise SourceSpecError(
                "URL contains a literal password; reference a secret instead, e.g. "
                "rtsp://user:${CAMERA_PASSWORD}@host/path"
            )
    for qk, qv in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if qk.lower() in _SENSITIVE_QUERY_KEYS and qv and not _is_placeholder(qv):
            raise SourceSpecError(
                f"URL query parameter {qk!r} looks like a credential; use ${{SECRET_NAME}} instead"
            )


def _looks_like_stream(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in _HTTP_SCHEMES:
        return True
    path = parts.path.lower()
    return path.endswith(_STREAM_PATH_HINTS) or "mjpg" in path or "mjpeg" in path


# ----------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class SourceSpec:
    """Typed, validated description of a frame source.

    Attributes:
        kind: one of ``camera``, ``stream``, ``snapshot``, ``screen``, ``folder``.
        target: camera index (int), URL template (str), monitor index (int) or folder path.
        name: display name; defaults to a redacted rendering of the target.
        region: screen region ``(left, top, width, height)`` relative to the monitor.
        timeout: open/read timeout in seconds (network and device probing).
        width, height: requested camera capture resolution (best effort).
        username_secret, password_secret: secret names for HTTP basic auth (snapshot).
        token_secret: secret name for an HTTP bearer token (snapshot).
    """

    kind: SourceKind
    target: int | str
    name: str | None = None
    region: tuple[int, int, int, int] | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    width: int | None = None
    height: int | None = None
    username_secret: str | None = None
    password_secret: str | None = None
    token_secret: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise SourceSpecError(f"unknown source kind {self.kind!r}")
        object.__setattr__(self, "timeout", _clamp_timeout(self.timeout))
        if self.kind in ("camera", "screen"):
            if isinstance(self.target, bool) or not isinstance(self.target, int):
                raise SourceSpecError(f"{self.kind} target must be an integer index")
            upper = MAX_CAMERA_INDEX if self.kind == "camera" else 32
            if not 0 <= self.target <= upper:
                raise SourceSpecError(f"{self.kind} index must be between 0 and {upper}")
        elif self.kind == "stream":
            _check_url_template(str(self.target), schemes=_STREAM_SCHEMES)
        elif self.kind == "snapshot":
            _check_url_template(str(self.target), schemes=_HTTP_SCHEMES)
        elif self.kind == "folder":
            if not isinstance(self.target, str) or not self.target.strip():
                raise SourceSpecError("folder target must be a non-empty path")
            if "\x00" in self.target:
                raise SourceSpecError("folder path contains a NUL byte")
        if self.region is not None:
            if self.kind != "screen":
                raise SourceSpecError("region is only valid for screen sources")
            if len(self.region) != 4:
                raise SourceSpecError("region must be (left, top, width, height)")
            left, top, w, h = (int(v) for v in self.region)
            if w <= 0 or h <= 0:
                raise SourceSpecError("region width and height must be positive")
            object.__setattr__(self, "region", (left, top, w, h))
        for dim in ("width", "height"):
            value = getattr(self, dim)
            if value is not None and (not isinstance(value, int) or not 0 < value <= 16384):
                raise SourceSpecError(f"{dim} must be a positive integer <= 16384")
        for attr in ("username_secret", "password_secret", "token_secret"):
            value = getattr(self, attr)
            if value is not None:
                if self.kind != "snapshot":
                    raise SourceSpecError(f"{attr} is only valid for snapshot sources")
                if not _SECRET_NAME_RE.match(value):
                    raise SourceSpecError(f"{attr} must be a secret name like CAM_PASSWORD")
        if (self.username_secret is None) != (self.password_secret is None):
            raise SourceSpecError("username_secret and password_secret must be given together")
        if self.token_secret and self.password_secret:
            raise SourceSpecError("use either basic auth secrets or token_secret, not both")

    # -- rendering -----------------------------------------------------------------

    @property
    def display_name(self) -> str:
        """Human-readable name that never contains a credential."""
        if self.name:
            return self.name
        return self.canonical()

    def canonical(self) -> str:
        """Spec string that round-trips through :meth:`parse` (credentials redacted)."""
        if self.kind == "camera":
            return f"camera:{self.target}"
        if self.kind == "screen":
            base = f"screen:{self.target}"
            if self.region:
                base += "@" + ",".join(str(v) for v in self.region)
            return base
        if self.kind == "folder":
            return f"folder:{self.target}"
        return f"{self.kind}:{redact_url(str(self.target))}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (credentials redacted, secret *names* kept)."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "spec": self.canonical(),
            "name": self.display_name,
        }
        if self.region:
            out["region"] = list(self.region)
        for attr in ("width", "height", "username_secret", "password_secret", "token_secret"):
            if getattr(self, attr) is not None:
                out[attr] = getattr(self, attr)
        out["timeout"] = self.timeout
        return out

    # -- parsing -------------------------------------------------------------------

    @classmethod
    def parse(
        cls,
        value: SourceSpec | str | int | Mapping[str, Any],
        *,
        named_streams: Mapping[str, str] | None = None,
    ) -> SourceSpec:
        """Build a spec from a spec string, camera index, mapping, or existing spec.

        ``named_streams`` is the ``stream_urls`` config (name -> URL template); a bare name
        or ``stream:<name>`` resolves through it.
        """
        if isinstance(value, SourceSpec):
            return value
        if isinstance(value, bool):
            raise SourceSpecError("source spec must be a string, index or mapping")
        if isinstance(value, int):
            return cls(kind="camera", target=value)
        if isinstance(value, Mapping):
            return cls._from_mapping(value, named_streams=named_streams)
        if not isinstance(value, str):
            raise SourceSpecError("source spec must be a string, index or mapping")
        return cls._from_string(value, named_streams=named_streams)

    @classmethod
    def _from_mapping(
        cls, data: Mapping[str, Any], *, named_streams: Mapping[str, str] | None
    ) -> SourceSpec:
        allowed = {f for f in cls.__dataclass_fields__} | {"spec", "url", "path", "index",
                                                             "monitor"}
        unknown = set(data) - allowed
        if unknown:
            raise SourceSpecError(f"unknown source fields: {', '.join(sorted(unknown))}")
        if "spec" in data:
            base = cls._from_string(str(data["spec"]), named_streams=named_streams)
        else:
            kind = data.get("kind")
            target = data.get("target")
            for alias in ("url", "path", "index", "monitor"):
                if target is None and alias in data:
                    target = data[alias]
            if kind is None or target is None:
                raise SourceSpecError("source mapping needs 'kind' and 'target' (or 'spec')")
            if kind in ("camera", "screen") and isinstance(target, str):
                target = _parse_index(target, kind)
            if kind == "stream" and isinstance(target, str) and named_streams:
                target = named_streams.get(target, target)
            base = cls(kind=kind, target=target)
        overrides: dict[str, Any] = {}
        for key in ("name", "timeout", "width", "height", "username_secret", "password_secret",
                    "token_secret"):
            if data.get(key) is not None:
                overrides[key] = data[key]
        if data.get("region") is not None:
            region = data["region"]
            if isinstance(region, str):
                region = _parse_region(region)
            try:
                overrides["region"] = tuple(int(v) for v in region)
            except (TypeError, ValueError) as exc:
                raise SourceSpecError("region must be four integers") from exc
        return replace(base, **overrides) if overrides else base

    @classmethod
    def _from_string(cls, text: str, *, named_streams: Mapping[str, str] | None) -> SourceSpec:
        raw = text.strip()
        if not raw:
            raise SourceSpecError("empty source spec")
        named = dict(named_streams or {})
        lowered = raw.lower()
        if raw.isdigit():
            return cls(kind="camera", target=int(raw))
        if raw in named:
            return cls._named_stream(raw, named[raw])
        prefix, sep, rest = raw.partition(":")
        prefix_l = prefix.lower()
        if lowered in ("camera", "webcam"):
            return cls(kind="camera", target=0)
        if lowered == "screen":
            return cls(kind="screen", target=1)
        if sep and prefix_l in ("camera", "webcam", "cam"):
            return cls(kind="camera", target=_parse_index(rest, "camera"))
        if sep and prefix_l in ("screen", "monitor"):
            index_text, at, region_text = rest.partition("@")
            index = _parse_index(index_text, "screen") if index_text.strip() else 1
            region = _parse_region(region_text) if at else None
            return cls(kind="screen", target=index, region=region)
        if sep and prefix_l in ("folder", "dir"):
            return cls(kind="folder", target=rest.strip())
        if sep and prefix_l == "snapshot":
            return cls(kind="snapshot", target=rest.strip())
        if sep and prefix_l == "stream":
            target = rest.strip()
            if target in named:
                return cls._named_stream(target, named[target])
            return cls(kind="stream", target=target)
        if sep and rest.startswith("//"):
            if prefix_l in _HTTP_SCHEMES and not _looks_like_stream(raw):
                return cls(kind="snapshot", target=raw)
            return cls(kind="stream", target=raw)
        if os.path.isdir(os.path.expanduser(raw)):
            return cls(kind="folder", target=raw)
        raise SourceSpecError(
            f"unrecognised source {redact_url(raw)!r}; use camera:N, screen[:N], a URL, "
            "folder:/path, or a configured stream name"
        )

    @classmethod
    def _named_stream(cls, name: str, url: str) -> SourceSpec:
        if not isinstance(url, str):
            raise SourceSpecError(f"configured stream {name!r} must map to a URL string")
        kind: SourceKind = "stream"
        target = url.strip()
        lowered = target.lower()
        if lowered.startswith("snapshot:"):
            kind, target = "snapshot", target.split(":", 1)[1].strip()
        elif lowered.startswith("stream:"):
            target = target.split(":", 1)[1].strip()
        elif lowered.split(":", 1)[0] in _HTTP_SCHEMES and not _looks_like_stream(target):
            kind = "snapshot"
        return cls(kind=kind, target=target, name=name)


def _parse_index(text: Any, kind: str) -> int:
    try:
        value = int(str(text).strip())
    except ValueError as exc:
        raise SourceSpecError(f"{kind} index must be an integer, got {text!r}") from exc
    return value


def _parse_region(text: str) -> tuple[int, int, int, int]:
    match = _REGION_RE.match(text or "")
    if not match:
        raise SourceSpecError("region must be 'left,top,width,height'")
    left, top, width, height = (int(g) for g in match.groups())
    return left, top, width, height


# ----------------------------------------------------------------------------- base


class _BaseSource:
    """Shared lifecycle, locking and context-manager support."""

    kind: str = "source"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        self.spec = spec
        self.clock: Callable[[], float] = clock or time.time
        self._lock = threading.RLock()
        self._is_open = False

    @property
    def name(self) -> str:
        return self.spec.display_name

    @property
    def is_open(self) -> bool:
        return self._is_open

    def __repr__(self) -> str:
        state = "open" if self._is_open else "closed"
        return f"<{type(self).__name__} {self.spec.canonical()} {state}>"

    def __enter__(self) -> _BaseSource:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if not self._is_open:
            raise SourceError(f"{self.name}: read() on a source that is not open")

    def open(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def read(self) -> tuple[float, Frame]:  # pragma: no cover - overridden
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# ----------------------------------------------------------------------------- OpenCV capture


class _CaptureSource(_BaseSource):
    """Common code for OpenCV ``VideoCapture`` based sources."""

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__(spec, clock=clock)
        self._cap: Any = None

    def _create_capture(self, cv2: ModuleType) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _configure(self, cv2: ModuleType, cap: Any) -> None:
        """Hook for per-kind capture properties after a successful open."""

    @staticmethod
    def _release(cap: Any) -> None:
        if cap is None:
            return
        try:
            cap.release()
        except Exception:  # noqa: BLE001 - release must never raise
            log.debug("VideoCapture.release failed", exc_info=True)

    def open(self) -> None:
        with self._lock:
            if self._is_open:
                return
            cv2 = _cv2(f"{self.kind} source {self.name!r}")

            def create() -> Any:
                cap = self._create_capture(cv2)
                if cap is None or not cap.isOpened():
                    self._release(cap)
                    return None
                return cap

            try:
                cap = _call_with_timeout(
                    create,
                    self.spec.timeout,
                    name=f"sentinel-open-{self.kind}",
                    on_abandoned=self._release,
                )
            except _Timeout as exc:
                raise SourceDisconnected(
                    f"{self.name}: timed out opening after {self.spec.timeout:.0f}s"
                ) from exc
            except SourceError:
                raise
            except Exception as exc:
                raise SourceDisconnected(f"{self.name}: cannot open ({exc})") from exc
            if cap is None:
                raise SourceDisconnected(f"{self.name}: cannot open {self.kind}")
            try:
                self._configure(cv2, cap)
            except Exception:  # noqa: BLE001 - properties are best effort
                log.debug("%s: setting capture properties failed", self.name, exc_info=True)
            self._cap = cap
            self._is_open = True

    def read(self) -> tuple[float, Frame]:
        with self._lock:
            self._require_open()
            try:
                ok, frame = self._cap.read()
            except Exception as exc:
                self._drop()
                raise SourceDisconnected(f"{self.name}: read failed ({exc})") from exc
            if not ok or frame is None:
                self._drop()
                raise SourceDisconnected(f"{self.name}: no frame (device or stream went away)")
            ts = self.clock()
            return ts, _as_bgr(frame, origin=self.name)

    def _drop(self) -> None:
        cap, self._cap = self._cap, None
        self._is_open = False
        self._release(cap)

    def close(self) -> None:
        with self._lock:
            self._drop()


class CameraSource(_CaptureSource):
    """Local camera by index via OpenCV (``cv2.VideoCapture(index)``)."""

    kind = "camera"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        if spec.kind != "camera":
            raise SourceSpecError(f"CameraSource needs a camera spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)

    def _create_capture(self, cv2: ModuleType) -> Any:
        return cv2.VideoCapture(int(self.spec.target))

    def _configure(self, cv2: ModuleType, cap: Any) -> None:
        # A one-frame buffer keeps reads fresh when the engine polls slower than the camera.
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if self.spec.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.spec.width)
        if self.spec.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.spec.height)


class StreamSource(_CaptureSource):
    """RTSP / HTTP(S) MJPEG / HLS stream via OpenCV's FFmpeg backend.

    Secret placeholders in the URL are resolved at ``open()``; the resolved URL is held only
    in the capture call and never stored or logged.
    """

    kind = "stream"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        if spec.kind != "stream":
            raise SourceSpecError(f"StreamSource needs a stream spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)

    def _create_capture(self, cv2: ModuleType) -> Any:
        url = _expand_url(str(self.spec.target), origin=self.name)
        ms = int(self.spec.timeout * 1000)
        params: list[int] = []
        for prop in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
            if hasattr(cv2, prop):
                params += [getattr(cv2, prop), ms]
        backend = getattr(cv2, "CAP_FFMPEG", 0)
        if params:
            try:
                return cv2.VideoCapture(url, backend, params)
            except TypeError:  # very old OpenCV without the params overload
                pass
        return cv2.VideoCapture(url, backend)


# ----------------------------------------------------------------------------- snapshot URL


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects but drop the Authorization header when the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_parts = urllib.parse.urlsplit(new.full_url)
        if new_parts.scheme.lower() not in _HTTP_SCHEMES:
            return None
        if new_parts.netloc.lower() != old_host:
            for key in list(new.headers):
                if key.lower() == "authorization":
                    del new.headers[key]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class SnapshotUrlSource(_BaseSource):
    """HTTP(S) endpoint that returns one JPEG/PNG per request (polled on every ``read``).

    Uses urllib with a timeout and a response size cap. Credentials come only from the
    secret scope: ``${NAME}`` placeholders in the URL, basic auth via ``username_secret`` /
    ``password_secret``, or a bearer token via ``token_secret``.
    """

    kind = "snapshot"

    def __init__(
        self,
        spec: SourceSpec,
        *,
        clock: Callable[[], float] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        if spec.kind != "snapshot":
            raise SourceSpecError(f"SnapshotUrlSource needs a snapshot spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        self._opener = opener or urllib.request.build_opener(_SameHostRedirectHandler())
        self._max_bytes = int(max_bytes)
        self._url: str | None = None
        self._headers: dict[str, str] = {}

    def __repr__(self) -> str:  # never show the resolved URL or headers
        return super().__repr__()

    def _auth_headers(self) -> dict[str, str]:
        spec = self.spec
        if spec.token_secret:
            token = _require_secret(spec.token_secret, origin=self.name)
            return {"Authorization": f"Bearer {token}"}
        if spec.username_secret and spec.password_secret:
            user = _require_secret(spec.username_secret, origin=self.name)
            password = _require_secret(spec.password_secret, origin=self.name)
            raw = f"{user}:{password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}

    def open(self) -> None:
        with self._lock:
            if self._is_open:
                return
            _cv2(f"snapshot source {self.name!r}")  # fail early with an install hint
            self._url = _expand_url(str(self.spec.target), origin=self.name)
            self._headers = {"User-Agent": _USER_AGENT, "Accept": "image/*",
                             **self._auth_headers()}
            self._is_open = True

    def _fetch(self) -> bytes:
        assert self._url is not None
        request = urllib.request.Request(self._url, headers=self._headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.spec.timeout) as resp:
                status = getattr(resp, "status", 200)
                if status is not None and not 200 <= int(status) < 300:
                    raise SourceDisconnected(f"{self.name}: HTTP {status}")
                length = resp.headers.get("Content-Length") if resp.headers else None
                if length and length.isdigit() and int(length) > self._max_bytes:
                    raise SourceError(f"{self.name}: image larger than {self._max_bytes} bytes")
                data = resp.read(self._max_bytes + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            with contextlib.suppress(Exception):
                exc.close()
            if code in (401, 403):
                raise SourceError(
                    f"{self.name}: HTTP {code} (check the configured secrets)"
                ) from None
            raise SourceDisconnected(f"{self.name}: HTTP {code}") from None
        except urllib.error.URLError as exc:
            raise SourceDisconnected(f"{self.name}: unreachable ({exc.reason})") from None
        except TimeoutError:
            raise SourceDisconnected(
                f"{self.name}: timed out after {self.spec.timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise SourceDisconnected(f"{self.name}: network error ({exc})") from None
        if len(data) > self._max_bytes:
            raise SourceError(f"{self.name}: image larger than {self._max_bytes} bytes")
        return data

    def read(self) -> tuple[float, Frame]:
        with self._lock:
            self._require_open()
            data = self._fetch()
            ts = self.clock()
            return ts, _decode_image(data, origin=self.name)

    def close(self) -> None:
        with self._lock:
            self._is_open = False
            self._url = None
            self._headers = {}


# ----------------------------------------------------------------------------- screen


def _mss_factory(purpose: str) -> tuple[Callable[[], Any], type[BaseException]]:
    mss = _import_optional("mss", purpose)
    factory = getattr(mss, "MSS", None) or mss.mss
    exc_mod = getattr(mss, "exception", None)
    err = getattr(exc_mod, "ScreenShotError", None) or getattr(mss, "ScreenShotError", Exception)
    return factory, err


class ScreenSource(_BaseSource):
    """Screen capture via mss: a whole monitor or a region of it.

    mss handles are thread-affine on some platforms (X11), so the handle is (re)created in
    whichever thread calls ``read()``.
    """

    kind = "screen"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        if spec.kind != "screen":
            raise SourceSpecError(f"ScreenSource needs a screen spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        self._sct: Any = None
        self._owner: int | None = None
        self._grab_box: dict[str, int] | None = None
        self._factory: Callable[[], Any] | None = None
        self._error_type: type[BaseException] = Exception

    def _handle(self) -> Any:
        ident = threading.get_ident()
        if self._sct is not None and self._owner == ident:
            return self._sct
        self._close_handle()
        assert self._factory is not None
        try:
            self._sct = self._factory()
        except Exception as exc:
            raise SourceDisconnected(
                f"{self.name}: cannot access the screen ({exc}); a graphical session is needed"
            ) from exc
        self._owner = ident
        return self._sct

    def _close_handle(self) -> None:
        sct, self._sct, self._owner = self._sct, None, None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                log.debug("mss close failed", exc_info=True)

    def _compute_box(self, sct: Any) -> dict[str, int]:
        monitors = list(getattr(sct, "monitors", []) or [])
        index = int(self.spec.target)
        if index >= len(monitors):
            raise SourceDisconnected(
                f"{self.name}: monitor {index} not found ({max(0, len(monitors) - 1)} available)"
            )
        mon = monitors[index]
        box = {k: int(mon[k]) for k in ("left", "top", "width", "height")}
        if self.spec.region:
            left, top, width, height = self.spec.region
            x0 = max(0, left)
            y0 = max(0, top)
            x1 = min(box["width"], left + width)
            y1 = min(box["height"], top + height)
            if x1 <= x0 or y1 <= y0:
                raise SourceSpecError(f"{self.name}: region lies outside monitor {index}")
            box = {"left": box["left"] + x0, "top": box["top"] + y0,
                   "width": x1 - x0, "height": y1 - y0}
        return box

    def open(self) -> None:
        with self._lock:
            if self._is_open:
                return
            _np(f"screen source {self.name!r}")
            self._factory, self._error_type = _mss_factory(f"screen source {self.name!r}")
            try:
                sct = self._handle()
                self._grab_box = self._compute_box(sct)
            except SourceError:
                self._close_handle()
                raise
            except Exception as exc:
                self._close_handle()
                raise SourceDisconnected(f"{self.name}: cannot open screen ({exc})") from exc
            self._is_open = True

    def read(self) -> tuple[float, Frame]:
        with self._lock:
            self._require_open()
            np = _np("screen capture")
            sct = self._handle()
            try:
                shot = sct.grab(self._grab_box)
                arr = np.asarray(shot)
            except Exception as exc:
                self._close_handle()
                raise SourceDisconnected(f"{self.name}: screen grab failed ({exc})") from exc
            ts = self.clock()
            return ts, _as_bgr(arr, origin=self.name)

    def close(self) -> None:
        with self._lock:
            self._is_open = False
            self._grab_box = None
            self._close_handle()


# ----------------------------------------------------------------------------- folder


@dataclass
class _FolderCache:
    key: tuple[str, int, int] | None = None
    frame: Any = None


class FolderSource(_BaseSource):
    """Newest image file (by modification time) in a directory.

    Each ``read`` rescans the directory (non-recursive, at most ``MAX_FOLDER_ENTRIES``
    entries) and returns the newest image. The last decoded frame is cached by
    (path, mtime, size), so an unchanged folder costs one directory scan. A newest file that
    cannot be decoded yet (still being written) falls back to the previous frame.
    """

    kind = "folder"

    def __init__(
        self,
        spec: SourceSpec,
        *,
        clock: Callable[[], float] | None = None,
        extensions: frozenset[str] = IMAGE_EXTENSIONS,
        max_file_bytes: int = MAX_IMAGE_FILE_BYTES,
    ) -> None:
        if spec.kind != "folder":
            raise SourceSpecError(f"FolderSource needs a folder spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        self.path = Path(os.path.expanduser(str(spec.target)))
        self._extensions = frozenset(e.lower() for e in extensions)
        self._max_file_bytes = int(max_file_bytes)
        self._cache = _FolderCache()

    def open(self) -> None:
        with self._lock:
            if self._is_open:
                return
            _cv2(f"folder source {self.name!r}")
            if not self.path.is_dir():
                raise SourceDisconnected(f"{self.name}: {self.path} is not a directory")
            self._cache = _FolderCache()
            self._is_open = True

    def newest(self) -> tuple[Path, int, int] | None:
        """Return ``(path, mtime_ns, size)`` of the newest image file, or ``None``."""
        best: tuple[int, str, int] | None = None
        try:
            with os.scandir(self.path) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_FOLDER_ENTRIES:
                        log.warning("%s: more than %d entries; scanning only the first",
                                    self.name, MAX_FOLDER_ENTRIES)
                        break
                    name = entry.name
                    if name.startswith(".") or Path(name).suffix.lower() not in self._extensions:
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
                        continue
                    candidate = (st.st_mtime_ns, name, st.st_size)
                    if best is None or candidate[:2] > best[:2]:
                        best = candidate
        except FileNotFoundError:
            raise SourceDisconnected(f"{self.name}: {self.path} no longer exists") from None
        except OSError as exc:
            raise SourceDisconnected(f"{self.name}: cannot list {self.path} ({exc})") from None
        if best is None:
            return None
        return self.path / best[1], best[0], best[2]

    def read(self) -> tuple[float, Frame]:
        with self._lock:
            self._require_open()
            found = self.newest()
            if found is None:
                raise SourceNotReady(f"{self.name}: no images in {self.path} yet")
            path, mtime_ns, size = found
            key = (path.name, mtime_ns, size)
            if key != self._cache.key:
                frame = self._load(path, size)
                if frame is None:
                    if self._cache.frame is None:
                        raise SourceNotReady(f"{self.name}: {path.name} is not readable yet")
                else:
                    self._cache = _FolderCache(key=key, frame=frame)
            ts = self.clock()
            return ts, self._cache.frame.copy()

    def _load(self, path: Path, size: int) -> Frame | None:
        if size > self._max_file_bytes:
            log.warning("%s: skipping %s (%d bytes > cap)", self.name, path.name, size)
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            return _decode_image(data, origin=f"{self.name}/{path.name}")
        except MissingDependency:
            raise
        except SourceError:
            return None

    def close(self) -> None:
        with self._lock:
            self._is_open = False
            self._cache = _FolderCache()


# ----------------------------------------------------------------------------- factory

_SOURCE_CLASSES: dict[str, type[_BaseSource]] = {
    "camera": CameraSource,
    "stream": StreamSource,
    "snapshot": SnapshotUrlSource,
    "screen": ScreenSource,
    "folder": FolderSource,
}


def open_source(
    spec: SourceSpec | str | int | Mapping[str, Any],
    *,
    named_streams: Mapping[str, str] | None = None,
    clock: Callable[[], float] | None = None,
    auto_open: bool = False,
) -> FrameSource:
    """Build the frame source for ``spec`` (see the module docstring for the syntax).

    The source is returned unopened unless ``auto_open`` is true, so callers control when
    devices are acquired. Raises :class:`SourceSpecError` for a bad spec.
    """
    parsed = SourceSpec.parse(spec, named_streams=named_streams)
    source = _SOURCE_CLASSES[parsed.kind](parsed, clock=clock)
    if auto_open:
        source.open()
    return source


# ----------------------------------------------------------------------------- probe


@dataclass(frozen=True)
class SourceInfo:
    """One entry of :func:`list_sources`.

    ``available`` is ``True``/``False`` when probed, ``None`` when not probed (configured
    network streams are listed without connecting).
    """

    spec: str
    kind: SourceKind
    name: str
    available: bool | None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"spec": self.spec, "kind": self.kind, "name": self.name,
                               "available": self.available, "detail": self.detail}
        out.update(self.extra)
        return out


_PROBE_LOCK = threading.Lock()
_probe_thread: threading.Thread | None = None


def _probe_cameras(max_index: int, per_camera_timeout: float, deadline: float) -> list[SourceInfo]:
    """Probe camera indexes 0..max_index in one worker thread, bounded by ``deadline``.

    Only one probe thread may exist at a time; if a previous probe is still stuck in a driver
    call, cameras are reported as not probed instead of starting another thread.
    """
    global _probe_thread
    try:
        cv2 = _cv2("camera probing")
    except MissingDependency as exc:
        return [SourceInfo(spec="camera:0", kind="camera", name="camera 0", available=False,
                           detail=str(exc))]

    results: dict[int, SourceInfo] = {}
    results_lock = threading.Lock()

    def probe_all() -> None:
        with contextlib.suppress(Exception):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        for index in range(max_index + 1):
            info = _probe_one_camera(cv2, index)
            with results_lock:
                results[index] = info

    with _PROBE_LOCK:
        if _probe_thread is not None and _probe_thread.is_alive():
            return [SourceInfo(spec=f"camera:{i}", kind="camera", name=f"camera {i}",
                               available=None, detail="previous camera probe still running")
                    for i in range(max_index + 1)]
        thread = threading.Thread(target=probe_all, name="sentinel-camera-probe", daemon=True)
        _probe_thread = thread
        thread.start()

    # Wait for the whole sweep, but never past the deadline nor more than per-camera budget
    # per index.
    budget = min(deadline - time.monotonic(), per_camera_timeout * (max_index + 1))
    thread.join(max(0.0, budget))
    out: list[SourceInfo] = []
    with results_lock:
        for index in range(max_index + 1):
            info = results.get(index)
            if info is None:
                info = SourceInfo(spec=f"camera:{index}", kind="camera", name=f"camera {index}",
                                  available=None, detail="probe timed out")
            out.append(info)
    return out


def _probe_one_camera(cv2: ModuleType, index: int) -> SourceInfo:
    spec, name = f"camera:{index}", f"camera {index}"
    cap = None
    try:
        cap = cv2.VideoCapture(index)
        if cap is None or not cap.isOpened():
            return SourceInfo(spec=spec, kind="camera", name=name, available=False,
                              detail="not found")
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        detail = f"{width}x{height}" if width and height else "available"
        return SourceInfo(spec=spec, kind="camera", name=name, available=True, detail=detail,
                          extra={"width": width, "height": height} if width else {})
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        return SourceInfo(spec=spec, kind="camera", name=name, available=False,
                          detail=f"error: {exc}")
    finally:
        _CaptureSource._release(cap)


def _probe_monitors(timeout: float) -> list[SourceInfo]:
    try:
        factory, _ = _mss_factory("screen capture")
    except MissingDependency as exc:
        return [SourceInfo(spec="screen:1", kind="screen", name="screen", available=False,
                           detail=str(exc))]

    def grab_monitors() -> list[dict[str, int]]:
        sct = factory()
        try:
            return [dict(m) for m in sct.monitors]
        finally:
            sct.close()

    try:
        monitors = _call_with_timeout(grab_monitors, timeout, name="sentinel-monitor-probe")
    except _Timeout:
        return [SourceInfo(spec="screen:1", kind="screen", name="screen", available=None,
                           detail="probe timed out")]
    except Exception as exc:  # noqa: BLE001 - no display, permission denied, ...
        return [SourceInfo(spec="screen:1", kind="screen", name="screen", available=False,
                           detail=f"screen capture unavailable ({exc})")]
    out: list[SourceInfo] = []
    for index, mon in enumerate(monitors):
        try:
            width, height = int(mon["width"]), int(mon["height"])
        except (KeyError, TypeError, ValueError):
            continue
        name = "all monitors" if index == 0 else f"monitor {index}"
        out.append(SourceInfo(spec=f"screen:{index}", kind="screen", name=name, available=True,
                              detail=f"{width}x{height}",
                              extra={"width": width, "height": height}))
    return out


def list_sources(
    *,
    named_streams: Mapping[str, str] | None = None,
    max_camera_index: int = 4,
    probe_cameras: bool = True,
    probe_screens: bool = True,
    per_camera_timeout: float = 3.0,
    total_timeout: float = 8.0,
) -> list[SourceInfo]:
    """List cameras (indexes ``0..max_camera_index``), monitors and configured streams.

    Never hangs: device probes run in daemon threads bounded by ``total_timeout``; anything
    not answered in time is reported with ``available=None`` and a "timed out" detail.
    Configured streams are listed (redacted) without connecting. Never raises for a probe
    failure; only a malformed ``named_streams`` entry is reported as unavailable.
    """
    max_camera_index = max(0, min(int(max_camera_index), MAX_CAMERA_INDEX))
    total_timeout = _clamp_timeout(total_timeout, 8.0)
    per_camera_timeout = _clamp_timeout(per_camera_timeout, 3.0)
    deadline = time.monotonic() + total_timeout
    out: list[SourceInfo] = []
    if probe_screens:
        out.extend(_probe_monitors(min(total_timeout, max(0.5, deadline - time.monotonic()))))
    if probe_cameras:
        out.extend(_probe_cameras(max_camera_index, per_camera_timeout, deadline))
    for name, url in sorted((named_streams or {}).items()):
        try:
            spec = SourceSpec._named_stream(str(name), url)
        except SourceSpecError as exc:
            out.append(SourceInfo(spec=f"stream:{name}", kind="stream", name=str(name),
                                  available=False, detail=f"invalid: {exc}"))
            continue
        out.append(SourceInfo(spec=str(name), kind=spec.kind, name=str(name), available=None,
                              detail=f"configured {spec.kind}: {redact_url(str(spec.target))}"))
    return out
