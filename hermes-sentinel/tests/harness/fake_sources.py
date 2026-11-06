"""Fake frame sources and a virtual clock for deterministic tests.

``FakeSource`` replays a ``Scene`` through the same ``FrameSource`` protocol the
real webcam/RTSP/screen sources implement (see ``sentinel.sources``). Time comes
from a ``FakeClock``, so a 10-minute clip "plays" instantly and timestamps are
exact.

    clock = FakeClock()
    src = FakeSource(build_scene("object_appears"), clock=clock)
    src.open()
    ts, frame = src.read()   # ts == 0.0, clock now at 0.1 (10 fps)
"""

from __future__ import annotations

import numpy as np

from .plugin_loader import plugin_module
from .scenes import Scene

# Through the loaded plugin package (hermes_plugins.<slug>), so these are the very classes the
# plugin raises and catches.
_sources = plugin_module("sentinel.sources")
SourceDisconnected = _sources.SourceDisconnected
SourceError = _sources.SourceError
SourceExhausted = _sources.SourceExhausted


class FakeClock:
    """Monotonic virtual clock. Nothing ever really sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    # Aliases so it can stand in for ``time.monotonic`` / ``time.time``.
    monotonic = now
    time = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        self._now += seconds
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))

    async def asleep(self, seconds: float) -> None:
        """Async stand-in for ``asyncio.sleep`` that only advances virtual time."""
        self.sleep(seconds)


class FakeSource:
    """Replays a scene at a virtual clock.

    Each ``read()`` returns ``(clock.now(), frame)`` and then advances the clock
    by one frame interval (``1 / fps``), unless ``advance_clock=False``, in which
    case the test drives the clock itself.

    Errors (all ``SourceError`` subclasses):
      * ``read()`` before ``open()`` or after ``close()``: ``SourceError``.
      * past ``disconnect_after`` frames: ``SourceDisconnected`` (and the source
        stays disconnected until re-opened).
      * past the end with ``loop=False``: ``SourceExhausted``.
    """

    def __init__(
        self,
        scene: Scene,
        clock: FakeClock | None = None,
        *,
        fps: float | None = None,
        loop: bool = False,
        advance_clock: bool = True,
        disconnect_after: int | None = None,
        open_error: Exception | None = None,
        name: str | None = None,
    ) -> None:
        self.scene = scene
        self.clock = clock if clock is not None else FakeClock()
        self.fps = float(fps if fps is not None else scene.fps)
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        self.loop = loop
        self.advance_clock = advance_clock
        self.disconnect_after = (
            disconnect_after if disconnect_after is not None else scene.disconnect_after
        )
        self.open_error = open_error
        self.name = name or f"fake:{scene.name}"

        self.is_open = False
        self.disconnected = False
        self.position = 0  # index of the next frame to deliver
        self.frames_read = 0
        self.open_count = 0
        self.close_count = 0

    @property
    def interval(self) -> float:
        return 1.0 / self.fps

    def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True
        self.disconnected = False
        self.position = 0
        self.open_count += 1

    def read(self) -> tuple[float, np.ndarray]:
        if not self.is_open:
            raise SourceError(f"{self.name}: read() on a source that is not open")
        if self.disconnected or (
            self.disconnect_after is not None and self.position >= self.disconnect_after
        ):
            self.disconnected = True
            raise SourceDisconnected(f"{self.name}: disconnected after {self.position} frames")
        if self.position >= len(self.scene.frames):
            if not self.loop:
                raise SourceExhausted(f"{self.name}: no more frames")
            self.position = 0
        ts = self.clock.now()
        frame = self.scene.frames[self.position].copy()  # callers may mutate
        self.position += 1
        self.frames_read += 1
        if self.advance_clock:
            self.clock.advance(self.interval)
        return ts, frame

    def close(self) -> None:
        if self.is_open:
            self.close_count += 1
        self.is_open = False

    def __enter__(self) -> FakeSource:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self):
        """Yield ``(ts, frame)`` until the source is exhausted or disconnects."""
        while True:
            try:
                yield self.read()
            except (SourceExhausted, SourceDisconnected):
                return
