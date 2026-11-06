"""Enumerate available cameras, monitors and configured streams without hanging."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from . import dependencies
from .backends import capture as capture_backend
from .backends import screen as screen_backend
from .constants import MAX_CAMERA_INDEX, SourceKind
from .errors import MissingDependency, SourceSpecError
from .spec import named_stream, redact_url
from .timeouts import CallTimeoutError, call_with_timeout, clamp_timeout


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
        """JSON-safe dict with ``extra`` merged in."""
        out: dict[str, Any] = {
            "spec": self.spec,
            "kind": self.kind,
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
        }
        out.update(self.extra)
        return out


_PROBE_LOCK = threading.Lock()  # guards _probe_thread
_probe_thread: threading.Thread | None = None


def _camera_info(index: int, available: bool | None, detail: str) -> SourceInfo:
    return SourceInfo(
        spec=f"camera:{index}",
        kind="camera",
        name=f"camera {index}",
        available=available,
        detail=detail,
    )


def _start_probe_thread(target: Callable[[], None]) -> threading.Thread | None:
    """Start the single camera probe thread, or return ``None`` if one is still running."""
    global _probe_thread
    with _PROBE_LOCK:
        if _probe_thread is not None and _probe_thread.is_alive():
            return None
        thread = threading.Thread(target=target, name="sentinel-camera-probe", daemon=True)
        _probe_thread = thread
        thread.start()
        return thread


def probe_camera_indexes(
    max_index: int, per_camera_timeout: float, deadline: float
) -> list[SourceInfo]:
    """Probe camera indexes 0..max_index in one worker thread, bounded by ``deadline``.

    Only one probe thread may exist at a time; if a previous probe is still stuck in a driver
    call, cameras are reported as not probed instead of starting another thread.
    """
    try:
        cv2 = dependencies.require_cv2("camera probing")
    except MissingDependency as exc:
        return [
            SourceInfo(
                spec="camera:0", kind="camera", name="camera 0", available=False, detail=str(exc)
            )
        ]

    results: dict[int, SourceInfo] = {}
    indexes = range(max_index + 1)
    results_lock = threading.Lock()  # guards results

    def probe_all() -> None:
        with contextlib.suppress(Exception):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        for index in indexes:
            info = probe_one_camera(cv2, index)
            with results_lock:
                results[index] = info

    thread = _start_probe_thread(probe_all)
    if thread is None:
        return [_camera_info(i, None, "previous camera probe still running") for i in indexes]

    # Wait for the whole sweep, but never past the deadline nor more than per-camera budget
    # per index.
    budget = min(deadline - time.monotonic(), per_camera_timeout * len(indexes))
    thread.join(max(0.0, budget))
    with results_lock:
        return [results.get(i) or _camera_info(i, None, "probe timed out") for i in indexes]


def probe_one_camera(cv2: ModuleType, index: int) -> SourceInfo:
    """Open camera ``index`` once and describe it. Never raises."""
    cap = None
    try:
        cap = cv2.VideoCapture(index)
        if cap is None or not cap.isOpened():
            return _camera_info(index, False, "not found")
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        detail = f"{width}x{height}" if width and height else "available"
        return SourceInfo(
            spec=f"camera:{index}",
            kind="camera",
            name=f"camera {index}",
            available=True,
            detail=detail,
            extra={"width": width, "height": height} if width else {},
        )
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        return _camera_info(index, False, f"error: {exc}")
    finally:
        capture_backend.CaptureSource.release_capture(cap)


def _screen_info(available: bool | None, detail: str) -> list[SourceInfo]:
    return [
        SourceInfo(
            spec="screen:1", kind="screen", name="screen", available=available, detail=detail
        )
    ]


def _monitor_infos(monitors: list[dict[str, int]]) -> list[SourceInfo]:
    out: list[SourceInfo] = []
    for index, mon in enumerate(monitors):
        try:
            width, height = int(mon["width"]), int(mon["height"])
        except (KeyError, TypeError, ValueError):
            continue
        name = "all monitors" if index == 0 else f"monitor {index}"
        out.append(
            SourceInfo(
                spec=f"screen:{index}",
                kind="screen",
                name=name,
                available=True,
                detail=f"{width}x{height}",
                extra={"width": width, "height": height},
            )
        )
    return out


def probe_monitors(timeout: float) -> list[SourceInfo]:
    """List monitors via mss within ``timeout`` seconds. Never raises."""
    try:
        factory, _ = screen_backend.mss_factory("screen capture")
    except MissingDependency as exc:
        return _screen_info(False, str(exc))

    def grab_monitors() -> list[dict[str, int]]:
        sct = factory()
        try:
            return [dict(m) for m in sct.monitors]
        finally:
            sct.close()

    try:
        monitors = call_with_timeout(grab_monitors, timeout, name="sentinel-monitor-probe")
    except CallTimeoutError:
        return _screen_info(None, "probe timed out")
    except Exception as exc:  # noqa: BLE001 - no display, permission denied, ...
        return _screen_info(False, f"screen capture unavailable ({exc})")
    return _monitor_infos(monitors)


def _configured_stream_info(name: Any, url: Any) -> SourceInfo:
    try:
        spec = named_stream(str(name), url)
    except SourceSpecError as exc:
        return SourceInfo(
            spec=f"stream:{name}",
            kind="stream",
            name=str(name),
            available=False,
            detail=f"invalid: {exc}",
        )
    return SourceInfo(
        spec=str(name),
        kind=spec.kind,
        name=str(name),
        available=None,
        detail=f"configured {spec.kind}: {redact_url(str(spec.target))}",
    )


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
    total_timeout = clamp_timeout(total_timeout, 8.0)
    per_camera_timeout = clamp_timeout(per_camera_timeout, 3.0)
    deadline = time.monotonic() + total_timeout
    out: list[SourceInfo] = []
    if probe_screens:
        out.extend(probe_monitors(min(total_timeout, max(0.5, deadline - time.monotonic()))))
    if probe_cameras:
        out.extend(probe_camera_indexes(max_camera_index, per_camera_timeout, deadline))
    for name, url in sorted((named_streams or {}).items()):
        out.append(_configured_stream_info(name, url))
    return out
