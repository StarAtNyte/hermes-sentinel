"""On-demand frames: the capture thread's newest one, or a one-off open/read/close."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from ...sources import SourceError, SourceNotReady
from ..infra import short_error
from ..state import EngineState

__all__ = ["FrameGrabber"]

READ_ATTEMPTS = 3
READ_RETRY_SECONDS = 0.2


class FrameGrabber:
    """Serves frames to tools and the dashboard without disturbing the capture threads."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (workers, source factory, frame age limit)."""
        self._state = state

    def grab(self, spec: str) -> Any:
        """A current frame of ``spec``.

        The capture thread's latest one if fresh, else a one-off open/read/close.

        Raises:
            SourceError: with a friendly message when no frame can be read.
        """
        state = self._state
        key = state.source_key(spec)
        with state.lock:
            worker = state.workers.get(key)
        stale = None
        if worker is not None:
            with worker.latest_lock:
                latest = worker.latest
            if latest is not None:
                if state.clock() - latest[0] <= state.settings.max_frame_age:
                    return latest[1]
                stale = latest[1]
        return self._one_off(spec, key, stale)

    def _one_off(self, spec: str, key: str, stale: Any) -> Any:
        """Open the source just for this call; fall back to ``stale`` if that fails."""
        source = None
        try:
            source = self._state.source_factory(spec)
            source.open()
            for _ in range(READ_ATTEMPTS):
                try:
                    _ts, frame = source.read()
                    return frame
                except SourceNotReady:
                    time.sleep(READ_RETRY_SECONDS)
            raise SourceNotReady(f"{key}: no frame available yet")
        except Exception as exc:  # noqa: BLE001 - mapped to SourceError below
            if stale is not None:
                return stale
            if isinstance(exc, SourceError):
                raise
            raise SourceError(f"{key}: {short_error(exc)}") from exc
        finally:
            if source is not None:
                with suppress(Exception):
                    source.close()
