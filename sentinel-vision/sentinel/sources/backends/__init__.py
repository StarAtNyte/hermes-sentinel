"""Concrete frame sources: OpenCV capture, snapshot URL, screen, folder and video file."""

from .capture import CameraSource, CaptureSource, StreamSource
from .folder import FolderSource
from .screen import ScreenSource
from .snapshot import SnapshotUrlSource
from .video_file import FileSource

__all__ = [
    "CameraSource",
    "CaptureSource",
    "FileSource",
    "FolderSource",
    "ScreenSource",
    "SnapshotUrlSource",
    "StreamSource",
]
