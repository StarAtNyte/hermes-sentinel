"""Real-event scenes whose change is gradual: a rising liquid level, a slowly opening door.

Per-frame change is far below the fast gate's threshold, but the cumulative change is a real
event the judge should see. ``labels`` mark the frame where the change is complete
(``changed=True``, ``bbox`` = final changed region); ``meta`` has ``start``, ``end`` and
``final_bbox``. A drift trigger must come after ``start`` and (for the sweep) by ``end + grace``.
"""

from __future__ import annotations

import numpy as np

from harness.scenes import (
    CAMERA_SIZE,
    DEFAULT_FPS,
    BBox,
    FrameLabel,
    Scene,
    _assemble,
    _background,
)

__all__ = ["DRIFT_SCENARIOS", "build_drift_scene", "liquid_rising", "door_opening"]

LIQUID_COLOR = (200, 120, 30)  # BGR blue
DOOR_COLOR = (40, 60, 90)
INTERIOR_COLOR = (225, 230, 235)


def _relabel(scene: Scene, start: int, end: int, final_bbox: BBox) -> Scene:
    """Replace per-frame labels with a single "change complete" label at ``end``."""
    scene.labels = [
        FrameLabel(
            index=i,
            changed=i == end,
            bbox=final_bbox if i == end else None,
            present=final_bbox if i >= start else None,
            note="gradual change complete" if i == end else "",
        )
        for i in range(len(scene.frames))
    ]
    scene.meta.update(start=start, end=end, final_bbox=final_bbox)
    return scene


def _ramp(n_frames: int, start: int, end: int) -> np.ndarray:
    """Progress in ``[0, 1]`` per frame: 0 before ``start``, linear to 1 at ``end``."""
    return np.clip((np.arange(n_frames) - start) / max(1, end - start), 0.0, 1.0)


def liquid_rising(
    seed: int = 0,
    n_frames: int = 200,
    start: int = 30,
    end: int = 150,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """A glass fills slowly (about one pixel row every four frames)."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *CAMERA_SIZE)
    x, y, w, h = 20, 15, 30, 44
    bg[y : y + h, x : x + w] = INTERIOR_COLOR
    bg[y : y + h, x - 1 : x] = 20.0
    bg[y : y + h, x + w : x + w + 1] = 20.0
    bg[y + h : y + h + 1, x - 1 : x + w + 1] = 20.0
    progress = _ramp(n_frames, start, end)
    content = []
    for p in progress:
        img = bg.copy()
        level = int(round(p * h))
        if level:
            img[y + h - level : y + h, x : x + w] = LIQUID_COLOR
        content.append(img)
    final = (x, y, w, h)
    scene = _assemble(
        name="liquid_rising",
        seed=seed,
        fps=fps,
        content=content,
        present=[None] * n_frames,
        rng=rng,
        should_trigger=True,
        description="A glass slowly fills with blue liquid.",
    )
    return _relabel(scene, start, end, final)


def door_opening(
    seed: int = 0,
    n_frames: int = 200,
    start: int = 30,
    end: int = 150,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """A door swings open slowly, revealing a bright interior one column at a time."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *CAMERA_SIZE)
    x, y, w, h = 100, 20, 36, 66
    bg[y : y + h, x : x + w] = DOOR_COLOR
    progress = _ramp(n_frames, start, end)
    content = []
    for p in progress:
        img = bg.copy()
        gap = int(round(p * (w - 6)))
        if gap:
            img[y : y + h, x : x + gap] = INTERIOR_COLOR
        content.append(img)
    scene = _assemble(
        name="door_opening",
        seed=seed,
        fps=fps,
        content=content,
        present=[None] * n_frames,
        rng=rng,
        should_trigger=True,
        description="A door slowly opens and the bright room behind it is revealed.",
    )
    return _relabel(scene, start, end, (x, y, w - 6, h))


DRIFT_SCENARIOS = {"liquid_rising": liquid_rising, "door_opening": door_opening}


def build_drift_scene(name: str, seed: int = 0, **kwargs) -> Scene:
    """Build a gradual-event scene by name. Raises KeyError for unknown names."""
    try:
        factory = DRIFT_SCENARIOS[name]
    except KeyError:
        raise KeyError(f"unknown drift scene {name!r}; known: {sorted(DRIFT_SCENARIOS)}") from None
    return factory(seed=seed, **kwargs)
