"""Region-of-interest and bounding-box geometry for the change gate."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

BBox = tuple[int, int, int, int]
"""``(x, y, w, h)`` in pixels of the frame given to :meth:`ChangeGate.process`."""

Roi = tuple[float, float, float, float]
"""``(x, y, w, h)`` as fractions of the frame width/height, each in ``[0, 1]``."""

__all__ = [
    "BBox",
    "Roi",
    "bbox_from_mask",
    "bbox_iou",
    "normalize_roi",
    "roi_from_pixels",
    "roi_slices",
]


def normalize_roi(roi: Sequence[float] | None) -> Roi | None:
    """Validate a fractional ROI ``(x, y, w, h)``; clip it to the unit square.

    Returns None for ``None`` (whole frame). Raises ``ValueError`` when the ROI is
    malformed, non-finite, or has no area inside the frame.
    """
    if roi is None:
        return None
    try:
        vals = [float(v) for v in roi]
    except (TypeError, ValueError):
        raise ValueError(f"roi must be 4 numbers (x, y, w, h), got {roi!r}") from None
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals):
        raise ValueError(f"roi must be 4 finite numbers (x, y, w, h), got {roi!r}")
    x, y, w, h = vals
    if w <= 0 or h <= 0:
        raise ValueError("roi width and height must be > 0")
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("roi lies outside the frame (use fractions of width/height)")
    return (x0, y0, x1 - x0, y1 - y0)


def roi_from_pixels(bbox: Sequence[float], frame_size: tuple[int, int]) -> Roi:
    """Convert a pixel box ``(x, y, w, h)`` on a ``(width, height)`` frame to a fractional ROI."""
    width, height = frame_size
    if width <= 0 or height <= 0:
        raise ValueError("frame_size must be positive")
    x, y, w, h = (float(v) for v in bbox)
    roi = normalize_roi((x / width, y / height, w / width, h / height))
    assert roi is not None
    return roi


def roi_slices(roi: Roi | None, width: int, height: int) -> tuple[slice, slice]:
    """Pixel slices (rows, cols) covering a fractional ROI on a ``width x height`` grid."""
    if roi is None:
        return slice(0, height), slice(0, width)
    x, y, w, h = roi
    x0 = min(width - 1, max(0, int(math.floor(x * width))))
    y0 = min(height - 1, max(0, int(math.floor(y * height))))
    x1 = max(x0 + 1, min(width, int(math.ceil((x + w) * width))))
    y1 = max(y0 + 1, min(height, int(math.ceil((y + h) * height))))
    return slice(y0, y1), slice(x0, x1)


def bbox_iou(a: BBox | None, b: BBox | None) -> float:
    """Intersection over union of two ``(x, y, w, h)`` boxes; 0.0 if either is None."""
    if a is None or b is None:
        return 0.0
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def bbox_from_mask(
    mask: Any,
    y_off: int,
    x_off: int,
    proc_size: tuple[int, int],
    frame_shape: tuple[int, int],
) -> BBox:
    """Bounding box of ``mask`` (processed coords, offset) mapped to input-frame pixels."""
    ys = np.flatnonzero(mask.any(axis=1))
    xs = np.flatnonzero(mask.any(axis=0))
    pw, ph = proc_size
    fh, fw = frame_shape
    sx, sy = fw / pw, fh / ph
    x0 = int(math.floor((xs[0] + x_off) * sx))
    y0 = int(math.floor((ys[0] + y_off) * sy))
    x1 = min(fw, int(math.ceil((xs[-1] + 1 + x_off) * sx)))
    y1 = min(fh, int(math.ceil((ys[-1] + 1 + y_off) * sy)))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))
