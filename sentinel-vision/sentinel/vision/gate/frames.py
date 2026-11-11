"""Frame validation and the grayscale / brightness-normalised form the gate compares."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import GateConfig
from .ops import ImageOps

__all__ = ["preprocess_frame", "validate_frame"]

_NORM_LEVEL = 128.0  # frames are rescaled so their mean sits here


def validate_frame(frame: Any) -> np.ndarray:
    """Return ``frame`` when it is a non-empty numeric 2-D/3-D image.

    Raises:
        ValueError: anything else.
    """
    if not isinstance(frame, np.ndarray):
        raise ValueError(f"frame must be a numpy array, got {type(frame).__name__}")
    if frame.ndim not in (2, 3) or frame.size == 0 or min(frame.shape[:2]) < 2:
        raise ValueError(f"frame must be a non-empty HxW[xC] image, got shape {frame.shape}")
    if frame.ndim == 3 and frame.shape[2] not in (1, 3, 4):
        raise ValueError(f"frame must have 1, 3 or 4 channels, got {frame.shape[2]}")
    if not (np.issubdtype(frame.dtype, np.integer) or np.issubdtype(frame.dtype, np.floating)):
        raise ValueError(f"frame dtype must be numeric, got {frame.dtype}")
    return frame


def preprocess_frame(frame: np.ndarray, ops: ImageOps, config: GateConfig) -> np.ndarray:
    """Grayscale, downscale, blur and brightness-normalise ``frame`` to float32."""
    gray = ops.to_gray(frame)
    if np.issubdtype(frame.dtype, np.integer) and frame.dtype.itemsize > 1:
        # 16-bit sources: scale into the 8-bit range the thresholds assume.
        gray = gray * np.float32(255.0 / np.iinfo(frame.dtype).max)
    small = ops.downscale(gray, config.max_width)
    small = ops.blur(small, config.blur_sigma)
    mean = float(small.mean())
    # Brightness normalisation: rejects global flicker/drift before any comparison.
    scale = _NORM_LEVEL / mean if mean > 1.0 else 1.0
    return np.ascontiguousarray(small * np.float32(scale), dtype=np.float32)
