"""OpenCV ``VideoCapture`` sources: local cameras and network streams."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import ModuleType
from typing import Any

from .. import dependencies
from ..base import BaseSource, Frame
from ..constants import STREAM_SCHEMES
from ..errors import SourceDisconnected, SourceError, SourceSpecError
from ..frames import as_bgr
from ..spec import SourceSpec, expand_url
from ..timeouts import CallTimeoutError, call_with_timeout

log = logging.getLogger(__name__)


class CaptureSource(BaseSource):
    """Common code for OpenCV ``VideoCapture`` based sources."""

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Bind the spec; the capture handle is created by ``open``."""
        super().__init__(spec, clock=clock)
        self._cap: Any = None

    def _create_capture(self, cv2: ModuleType) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _configure(self, cv2: ModuleType, cap: Any) -> None:
        """Hook for per-kind capture properties after a successful open."""

    @staticmethod
    def release_capture(cap: Any) -> None:
        """Release a ``VideoCapture`` handle, never raising."""
        if cap is None:
            return
        try:
            cap.release()
        except Exception:  # noqa: BLE001 - release must never raise
            log.debug("VideoCapture.release failed", exc_info=True)

    def _open_capture(self, cv2: ModuleType) -> Any:
        """Create the capture with a deadline; returns ``None`` if it did not open."""

        def create() -> Any:
            cap = self._create_capture(cv2)
            if cap is None or not cap.isOpened():
                self.release_capture(cap)
                return None
            return cap

        try:
            return call_with_timeout(
                create,
                self.spec.timeout,
                name=f"sentinel-open-{self.kind}",
                on_abandoned=self.release_capture,
            )
        except CallTimeoutError as exc:
            raise SourceDisconnected(
                f"{self.name}: timed out opening after {self.spec.timeout:.0f}s"
            ) from exc
        except SourceError:
            raise
        except Exception as exc:
            raise SourceDisconnected(f"{self.name}: cannot open ({exc})") from exc

    def open(self) -> None:
        """Open the capture, bounded by the spec's timeout."""
        with self._lock:
            if self._is_open:
                return
            cv2 = dependencies.require_cv2(f"{self.kind} source {self.name!r}")
            cap = self._open_capture(cv2)
            if cap is None:
                raise SourceDisconnected(f"{self.name}: cannot open {self.kind}")
            try:
                self._configure(cv2, cap)
            except Exception:  # noqa: BLE001 - properties are best effort
                log.debug("%s: setting capture properties failed", self.name, exc_info=True)
            self._cap = cap
            self._is_open = True

    def read(self) -> tuple[float, Frame]:
        """Grab one frame; a failed read drops the handle and raises ``SourceDisconnected``."""
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
            return ts, as_bgr(frame, origin=self.name)

    def _drop(self) -> None:
        cap, self._cap = self._cap, None
        self._is_open = False
        self.release_capture(cap)

    def close(self) -> None:
        """Release the capture handle."""
        with self._lock:
            self._drop()


class CameraSource(CaptureSource):
    """Local camera by index via OpenCV (``cv2.VideoCapture(index)``)."""

    kind = "camera"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Require a camera spec."""
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


class StreamSource(CaptureSource):
    """RTSP / HTTP(S) MJPEG / HLS stream via OpenCV's FFmpeg backend.

    Secret placeholders in the URL are resolved at ``open()``; the resolved URL is held only
    in the capture call and never stored or logged.
    """

    kind = "stream"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Require a stream spec."""
        if spec.kind != "stream":
            raise SourceSpecError(f"StreamSource needs a stream spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)

    def _create_capture(self, cv2: ModuleType) -> Any:
        url = expand_url(
            str(self.spec.target),
            origin=self.name,
            schemes=STREAM_SCHEMES,
            configured=self.spec.configured,
        )
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
