"""Concrete frame sources: OpenCV capture, snapshot URL, screen and folder."""

from .capture import CameraSource, CaptureSource, StreamSource
from .folder import FolderSource
from .screen import ScreenSource
from .snapshot import SnapshotUrlSource

__all__ = [
    "CameraSource",
    "CaptureSource",
    "FolderSource",
    "ScreenSource",
    "SnapshotUrlSource",
    "StreamSource",
]
