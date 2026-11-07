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
``file:/path/clip.mp4`` or a bare      local video file (.mp4 .mkv .mov .avi .webm .m4v), for demos
``clip.mp4`` path                      and repeatable tests; ``?loop=true&speed=3`` restarts at the
                                       end / delivers every 3rd frame; finishes with SourceExhausted
``<name>`` / ``stream:<name>``         a configured stream (``stream_urls`` config)
=====================================  ==========================================================

Not supported, on purpose: audio (Sentinel is vision-only) and video-site streams such as
YouTube (extracting them breaks site terms and needs downloaded binaries; use a file or a
camera URL you own instead).

A ``${NAME}`` may also be a whole URL stored as a secret (``stream_urls: {door:
"${SENTINEL_STREAM_URL_1}"}``; ``snapshot:${SENTINEL_STREAM_URL_2}`` for JPEG endpoints).

Credentials never live in specs or config. A URL may reference secrets as ``${NAME}``
placeholders (``rtsp://admin:${DOORCAM_PASSWORD}@10.0.0.5/live``); they are resolved through
``agent.secret_scope.get_secret`` at ``open()`` time, never written back, and never shown in
errors or descriptions. On a multiplexing gateway a boot thread has no secret scope; the last
value read inside a tool or command call is then served from an in-memory cache.
A literal password in a URL is rejected. Snapshot sources can also use HTTP basic auth
(``username_secret`` / ``password_secret``) or a bearer token (``token_secret``).

OpenCV and mss are imported lazily, so the plugin loads without them; a source that needs a
missing package raises :class:`MissingDependency` with an install hint.
"""

from .backends import (
    CameraSource,
    CaptureSource,
    FileSource,
    FolderSource,
    ScreenSource,
    SnapshotUrlSource,
    StreamSource,
)
from .base import BaseSource, Frame, FrameSource
from .constants import (
    IMAGE_EXTENSIONS,
    MAX_CAMERA_INDEX,
    SOURCE_KINDS,
    STREAM_URL_SLOTS,
    VIDEO_EXTENSIONS,
    SourceKind,
)
from .dependencies import dependency_status
from .discovery import SourceInfo, list_sources
from .errors import (
    MissingDependency,
    SecretScopeUnavailable,
    SourceDisconnected,
    SourceError,
    SourceExhausted,
    SourceNotReady,
    SourceSpecError,
)
from .registry import SOURCE_CLASSES, open_source
from .spec import (
    SecretStatus,
    SourceSpec,
    clear_cached_secrets,
    prime_secrets,
    probe_secrets,
    redact_url,
    referenced_secrets,
)
from .timeouts import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "IMAGE_EXTENSIONS",
    "MAX_CAMERA_INDEX",
    "MAX_TIMEOUT_S",
    "SOURCE_CLASSES",
    "SOURCE_KINDS",
    "STREAM_URL_SLOTS",
    "VIDEO_EXTENSIONS",
    "BaseSource",
    "CameraSource",
    "CaptureSource",
    "FileSource",
    "FolderSource",
    "Frame",
    "FrameSource",
    "MissingDependency",
    "ScreenSource",
    "SecretScopeUnavailable",
    "SecretStatus",
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
    "clear_cached_secrets",
    "dependency_status",
    "list_sources",
    "open_source",
    "prime_secrets",
    "probe_secrets",
    "redact_url",
    "referenced_secrets",
]
