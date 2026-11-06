"""Shared image helpers: resize, JPEG/PNG encoding and ROI cropping.

cv2 is used for resizing and JPEG encoding when available. Without it, frames are
area-downscaled with numpy and sent as PNG (zlib). numpy is imported lazily so the plugin
loads without it.
"""

from __future__ import annotations

import logging
import math
import struct
import zlib
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["crop_roi", "encode_image", "encode_jpeg"]

DEFAULT_MAX_SIDE = 1024
DEFAULT_JPEG_QUALITY = 85
#: Only add the ROI close-up when the ROI covers less than this fraction of the frame.
ROI_CROP_MAX_AREA = 0.6
MIN_CROP_SIDE = 16


def _np() -> Any:
    import numpy as np  # local: the plugin must load without numpy

    return np


def _cv2() -> Any | None:
    try:
        import cv2
    except Exception:  # noqa: BLE001 - ImportError or a broken binary wheel
        return None
    return cv2


def _as_bgr_uint8(frame: Any) -> Any:
    """HxW / HxWx1 / HxWx3 / HxWx4 array of any numeric dtype -> contiguous HxWx3 uint8."""
    np = _np()
    arr = np.asarray(frame)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[..., 0]
    if arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] not in (3, 4) or arr.shape[0] < 1 or arr.shape[1] < 1:
        raise ValueError(f"unsupported frame shape {getattr(arr, 'shape', None)}")
    arr = arr[..., :3]
    if arr.dtype != np.uint8:
        data = np.nan_to_num(arr.astype(np.float64), nan=0.0, posinf=255.0, neginf=0.0)
        if arr.dtype.kind == "f" and data.size and float(data.max()) <= 1.0:
            data = data * 255.0
        elif arr.dtype.kind in "ui" and arr.dtype.itemsize > 1:
            data = data / 257.0
        arr = np.clip(np.rint(data), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def _resize_max_side(img: Any, max_side: int) -> Any:
    """Downscale so the longest side is at most ``max_side`` (never upscales)."""
    np = _np()
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    cv2 = _cv2()
    if cv2 is not None:
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    # numpy fallback: integer block mean, then nearest-neighbour trim to the target size.
    factor = math.ceil(longest / max_side)
    hh, ww = (h // factor) * factor, (w // factor) * factor
    small = img[:hh, :ww].reshape(hh // factor, factor, ww // factor, factor, 3).mean(axis=(1, 3))
    return np.ascontiguousarray(np.clip(np.rint(small), 0, 255).astype(np.uint8))


def _png_bytes(img: Any) -> bytes:
    """Minimal RGB PNG encoder (zlib) used when cv2 is not installed."""
    np = _np()
    rgb = img[..., ::-1]  # BGR -> RGB
    h, w = rgb.shape[:2]
    raw = np.empty((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 0] = 0  # filter type "None" per scanline
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def encode_image(
    frame: Any, *, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_JPEG_QUALITY
) -> tuple[bytes, str]:
    """Encode a frame for the model: ``(bytes, mime_type)``.

    JPEG via cv2 when available, otherwise PNG. The longest side is at most ``max_side``.
    Raises ``ValueError`` for an unusable frame.
    """
    img = _resize_max_side(_as_bgr_uint8(frame), max(16, int(max_side)))
    cv2 = _cv2()
    if cv2 is not None:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return bytes(buf.tobytes()), "image/jpeg"
    return _png_bytes(img), "image/png"


def encode_jpeg(
    frame: Any, *, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_JPEG_QUALITY
) -> bytes | None:
    """JPEG bytes for storage (snapshots, examples).

    Returns ``None`` when cv2 is unavailable or the frame is unusable. Never raises.
    """
    try:
        data, mime = encode_image(frame, max_side=max_side, quality=quality)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentinel: could not encode frame: %s", exc)
        return None
    return data if mime == "image/jpeg" else None


def crop_roi(frame: Any, roi: Sequence[float] | None) -> Any | None:
    """Crop ``roi`` (fractions ``x, y, w, h``) out of ``frame``.

    Returns ``None`` when there is no ROI, it covers most of the frame, or the crop would be
    tiny.
    """
    if roi is None:
        return None
    try:
        x, y, w, h = (float(v) for v in roi)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    if w * h >= ROI_CROP_MAX_AREA:
        return None
    img = _as_bgr_uint8(frame)
    fh, fw = img.shape[:2]
    x0, y0 = max(0, int(math.floor(x * fw))), max(0, int(math.floor(y * fh)))
    x1, y1 = min(fw, int(math.ceil((x + w) * fw))), min(fh, int(math.ceil((y + h) * fh)))
    if x1 - x0 < MIN_CROP_SIDE or y1 - y0 < MIN_CROP_SIDE:
        return None
    return img[y0:y1, x0:x1]
