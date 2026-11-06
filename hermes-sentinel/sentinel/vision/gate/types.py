"""Result and state types of the change gate."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import BBox

__all__ = ["GateResult", "GateState", "Reference"]


@dataclass(frozen=True)
class GateResult:
    """Outcome of one :meth:`ChangeGate.process` call.

    Attributes:
        changed: a real change was detected inside the ROI on this frame.
        ratio: changed fraction of the ROI (after opening), in ``[0, 1]``.
        bbox: bounding box of the changed pixels inside the ROI, in input-frame pixels,
            or None when nothing changed.
        tick: the periodic idle check is due; judge the frame even though it did not change.
        rebaselined: the background was reset (first frame, size change, global change).
        reason: short machine-readable cause: ``"change"``, ``"quiet"``, ``"warmup"``,
            ``"global_change"`` or ``"resized"``.
        score_threshold: the per-pixel score threshold used (for diagnostics).
    """

    changed: bool
    ratio: float
    bbox: BBox | None
    tick: bool = False
    rebaselined: bool = False
    reason: str = "quiet"
    score_threshold: float = 0.0

    @property
    def should_judge(self) -> bool:
        """True when this frame should go to the vision judge (change or tick)."""
        return self.changed or self.tick


# ------------------------------------------------------------------------ ROI


@dataclass
class Reference:
    """A reference image plus its cached local statistics."""

    img: np.ndarray
    mean: np.ndarray
    var: np.ndarray


@dataclass
class GateState:
    """Mutable per-gate detection state (guarded by the owning gate's lock)."""

    shape: tuple[int, int] | None = None  # input frame size as (height, width)
    prev: Reference | None = None
    background: np.ndarray | None = None
    background_ref: Reference | None = None
    last_tick: float | None = None
    noise_ref: np.ndarray | None = None  # running per-brightness-bin noise level
    since_baseline: int = 0  # frames blended into the background since the last baseline
    stats: dict[str, int] = field(
        default_factory=lambda: {"frames": 0, "changed": 0, "ticks": 0, "rebaselines": 0}
    )
