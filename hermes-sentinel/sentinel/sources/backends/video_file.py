"""Local video-file source: replay a recording as if it were a camera (demos, repeatable tests)."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from ..base import Frame
from ..errors import (
    SourceDisconnected,
    SourceExhausted,
    SourceSpecError,
)
from ..frames import as_bgr
from ..spec import SourceSpec
from .capture import CaptureSource

__all__ = ["FileSource", "check_video_path"]


def check_video_path(path: Path, *, origin: str) -> None:
    """Require ``path`` to be an existing, readable, regular file (never a device or pipe).

    Raises:
        SourceDisconnected: the file does not exist (it may appear later).
        SourceSpecError: the path is not a regular readable file.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise SourceDisconnected(f"{origin}: {path} does not exist") from None
    except OSError as exc:
        raise SourceDisconnected(f"{origin}: cannot stat {path} ({exc})") from None
    if not stat.S_ISREG(mode):
        raise SourceSpecError(f"{origin}: {path} is not a regular file")
    if not os.access(path, os.R_OK):
        raise SourceSpecError(f"{origin}: {path} is not readable")


class FileSource(CaptureSource):
    """A local video file read through OpenCV; no network is ever involved.

    ``spec.speed`` is a frame step (every Nth frame is delivered). At the end the video
    restarts when ``spec.loop`` is set; otherwise ``read`` raises :class:`SourceExhausted`
    and the source closes, so callers can tell "finished" from "went away".
    """

    kind = "file"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Require a file spec and resolve its path to an absolute one."""
        if spec.kind != "file":
            raise SourceSpecError(f"FileSource needs a file spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        # Absolute, so OpenCV can never read it as a protocol such as ``rtsp:`` or ``concat:``.
        self.path = Path(os.path.abspath(os.path.expanduser(str(spec.target))))
        self._cv2: ModuleType | None = None

    def _create_capture(self, cv2: ModuleType) -> Any:
        check_video_path(self.path, origin=self.name)
        self._cv2 = cv2
        return cv2.VideoCapture(str(self.path))

    def read(self) -> tuple[float, Frame]:
        """Deliver the next frame (after skipping ``speed - 1``); see the class docstring."""
        with self._lock:
            self._require_open()
            frame = self._next_frame(allow_rewind=self.spec.loop)
            return self.clock(), as_bgr(frame, origin=self.name)

    def _next_frame(self, *, allow_rewind: bool) -> Any:
        """Skip to the next wanted frame, rewinding once at the end when allowed."""
        try:
            ok, frame = self._grab_stepped()
        except Exception as exc:
            self._drop()
            raise SourceDisconnected(f"{self.name}: read failed ({exc})") from exc
        if ok and frame is not None:
            return frame
        if allow_rewind and self._rewind():
            return self._next_frame(allow_rewind=False)
        self._drop()
        raise SourceExhausted(f"{self.name}: reached the end of {self.path.name}")

    def _grab_stepped(self) -> tuple[bool, Any]:
        for _ in range(self.spec.speed - 1):
            if not self._cap.grab():
                return False, None
        return self._cap.read()

    def _rewind(self) -> bool:
        """Seek back to the first frame; ``False`` if the file cannot be rewound."""
        position = getattr(self._cv2, "CAP_PROP_POS_FRAMES", 1)
        return bool(self._cap.set(position, 0))
