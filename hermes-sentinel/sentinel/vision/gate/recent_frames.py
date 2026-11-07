"""A small, memory-capped ring buffer of recent frames for motion sequences."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import GateConfig

__all__ = ["RecentFrames", "TimedFrame"]


@dataclass(frozen=True)
class TimedFrame:
    """A kept frame: capture time in seconds and a read-only ``uint8`` image."""

    ts: float
    frame: np.ndarray


def subsample(frame: np.ndarray, max_width: int) -> np.ndarray:
    """Copy of ``frame`` as ``uint8``, strided down so it is at most ``max_width`` wide."""
    step = max(1, -(-frame.shape[1] // max_width))
    small = frame[::step, ::step]
    if np.issubdtype(small.dtype, np.floating):
        small = np.nan_to_num(small, nan=0.0, posinf=255.0, neginf=0.0)
    if small.dtype != np.uint8:
        if np.issubdtype(small.dtype, np.integer) and small.dtype.itemsize > 1:
            small = small.astype(np.float32) * np.float32(255.0 / np.iinfo(small.dtype).max)
        small = np.clip(small, 0, 255)
    out = np.array(small, dtype=np.uint8, order="C", copy=True)
    out.setflags(write=False)
    return out


class RecentFrames:
    """Keeps the last few frames, spaced apart in time, within a byte budget.

    Not thread safe by itself: the owning gate calls it under its lock. Frames are decoded
    numpy arrays (no JPEG), so the judge can build a motion sequence without a second decode.
    """

    def __init__(self) -> None:
        """Create an empty buffer."""
        self._items: deque[TimedFrame] = deque()
        self._bytes = 0

    def clear(self) -> None:
        """Drop every kept frame."""
        self._items.clear()
        self._bytes = 0

    def offer(self, frame: np.ndarray, ts: float, cfg: GateConfig) -> bool:
        """Keep ``frame`` if enough time passed since the last kept one; return whether kept."""
        if cfg.recent_frames <= 0 or cfg.recent_max_bytes <= 0:
            self.clear()
            return False
        last = self._items[-1].ts if self._items else None
        if last is not None and last <= ts < last + cfg.recent_spacing_seconds:
            return False
        if last is not None and ts < last:  # clock restarted: old frames are not a sequence
            self.clear()
        item = TimedFrame(ts, subsample(frame, cfg.recent_max_width))
        self._items.append(item)
        self._bytes += item.frame.nbytes
        self._trim(cfg)
        return True

    def _trim(self, cfg: GateConfig) -> None:
        while len(self._items) > cfg.recent_frames or (
            len(self._items) > 1 and self._bytes > cfg.recent_max_bytes
        ):
            self._bytes -= self._items.popleft().frame.nbytes
        if self._items and self._bytes > cfg.recent_max_bytes:
            self.clear()  # a single frame larger than the whole budget is not kept

    def latest(self, count: int | None = None) -> list[TimedFrame]:
        """The newest ``count`` frames (all when None), oldest first."""
        items = list(self._items)
        return items if count is None else items[max(0, len(items) - max(0, count)) :]
