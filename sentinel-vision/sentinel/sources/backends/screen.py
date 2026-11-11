"""Screen capture source (mss): a whole monitor or a region of it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .. import dependencies
from ..base import BaseSource, Frame
from ..errors import SourceDisconnected, SourceError, SourceSpecError
from ..frames import as_bgr
from ..spec import SourceSpec

log = logging.getLogger(__name__)


def mss_factory(purpose: str) -> tuple[Callable[[], Any], type[BaseException]]:
    """Return mss' screenshot factory and its error type, or raise ``MissingDependency``."""
    mss = dependencies.import_optional("mss", purpose)
    factory = getattr(mss, "MSS", None) or mss.mss
    exc_mod = getattr(mss, "exception", None)
    err = getattr(exc_mod, "ScreenShotError", None) or getattr(mss, "ScreenShotError", Exception)
    return factory, err


class ScreenSource(BaseSource):
    """Screen capture via mss: a whole monitor or a region of it.

    mss handles are thread-affine on some platforms (X11), so the handle is (re)created in
    whichever thread calls ``read()``.
    """

    kind = "screen"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Require a screen spec."""
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
            except Exception:  # noqa: BLE001 - broad: third-party mss close, logged below
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
        if not self.spec.region:
            return box
        left, top, width, height = self.spec.region
        x0 = max(0, left)
        y0 = max(0, top)
        x1 = min(box["width"], left + width)
        y1 = min(box["height"], top + height)
        if x1 <= x0 or y1 <= y0:
            raise SourceSpecError(f"{self.name}: region lies outside monitor {index}")
        return {
            "left": box["left"] + x0,
            "top": box["top"] + y0,
            "width": x1 - x0,
            "height": y1 - y0,
        }

    def open(self) -> None:
        """Create the mss handle and fix the grab box."""
        with self._lock:
            if self._is_open:
                return
            dependencies.require_numpy(f"screen source {self.name!r}")
            self._factory, self._error_type = mss_factory(f"screen source {self.name!r}")
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
        """Grab the configured box as a BGR frame."""
        with self._lock:
            self._require_open()
            np = dependencies.require_numpy("screen capture")
            sct = self._handle()
            try:
                shot = sct.grab(self._grab_box)
                arr = np.asarray(shot)
            except Exception as exc:
                self._close_handle()
                raise SourceDisconnected(f"{self.name}: screen grab failed ({exc})") from exc
            ts = self.clock()
            return ts, as_bgr(arr, origin=self.name)

    def close(self) -> None:
        """Release the mss handle."""
        with self._lock:
            self._is_open = False
            self._grab_box = None
            self._close_handle()
