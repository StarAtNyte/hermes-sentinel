"""Pick the frames a motion rule is shown."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..vision.judge import SequenceFrame

__all__ = ["motion_sequence"]

#: An earlier frame closer than this to the current one adds nothing to a motion view.
MIN_GAP_SECONDS = 0.25


def motion_sequence(
    recent: Sequence[Any], frame: Any, ts: float, max_frames: int
) -> tuple[SequenceFrame, ...]:
    """Earlier frames (``ts``/``frame`` objects, oldest first) plus the current ``frame``.

    Returns up to ``max_frames`` frames in time order with offsets relative to ``ts``, or an
    empty tuple when there is nothing earlier to compare with (the caller then sends a still).
    """
    if max_frames < 2:
        return ()
    earlier = [item for item in recent if item.ts < ts - MIN_GAP_SECONDS]
    earlier = earlier[-(max_frames - 1) :]
    if not earlier:
        return ()
    frames = [SequenceFrame(offset=item.ts - ts, frame=item.frame) for item in earlier]
    frames.append(SequenceFrame(offset=0.0, frame=frame))
    return tuple(frames)
