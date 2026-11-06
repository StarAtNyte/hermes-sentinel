"""Source kinds, URL schemes and size limits shared across the sources package."""

from __future__ import annotations

from typing import Literal

SourceKind = Literal["camera", "stream", "snapshot", "screen", "folder"]
SOURCE_KINDS: tuple[str, ...] = ("camera", "stream", "snapshot", "screen", "folder")

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
)
STREAM_PATH_HINTS = (".mjpg", ".mjpeg", ".m3u8", ".mp4", ".mkv", ".webm", ".flv", ".ts")
STREAM_SCHEMES = frozenset({"rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "udp", "tcp"})
HTTP_SCHEMES = frozenset({"http", "https"})

MAX_CAMERA_INDEX = 64
MAX_SCREEN_INDEX = 32
MAX_DIMENSION = 16384
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_FILE_BYTES = 50 * 1024 * 1024
MAX_FOLDER_ENTRIES = 10_000
