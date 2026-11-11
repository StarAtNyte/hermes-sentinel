"""Normalise decoded images to the BGR uint8 frames the rest of the plugin expects."""

from __future__ import annotations

from typing import Any

from . import dependencies
from .base import Frame
from .errors import SourceError


def _to_uint8(arr: Any, np: Any, origin: str) -> Any:
    if arr.dtype == np.uint8:
        return arr
    if arr.dtype == np.uint16:
        return (arr >> 8).astype(np.uint8)
    raise SourceError(f"{origin}: unsupported pixel type {arr.dtype}")


def _to_three_channels(arr: Any, np: Any, origin: str) -> Any:
    if arr.ndim == 2:
        return np.repeat(arr[:, :, None], 3, axis=2)
    if arr.ndim == 3 and arr.shape[2] == 1:
        return np.repeat(arr, 3, axis=2)
    if arr.ndim == 3 and arr.shape[2] == 4:
        return arr[:, :, :3]
    if arr.ndim == 3 and arr.shape[2] == 3:
        return arr
    raise SourceError(f"{origin}: unsupported frame shape {arr.shape}")


def as_bgr(frame: Any, *, origin: str) -> Frame:
    """Coerce a decoded image to a contiguous HxWx3 uint8 BGR array or raise SourceError."""
    np = dependencies.require_numpy("frame handling")
    if frame is None:
        raise SourceError(f"{origin}: no image data")
    arr = np.asarray(frame)
    if arr.size == 0:
        raise SourceError(f"{origin}: empty frame")
    arr = _to_uint8(arr, np, origin)
    return np.ascontiguousarray(_to_three_channels(arr, np, origin))


def decode_image(data: bytes, *, origin: str) -> Frame:
    """Decode encoded image bytes (JPEG/PNG/...) to BGR."""
    cv2 = dependencies.require_cv2("decoding images")
    np = dependencies.require_numpy("decoding images")
    if not data:
        raise SourceError(f"{origin}: empty image data")
    buf = np.frombuffer(data, dtype=np.uint8)
    try:
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as exc:  # noqa: BLE001 - broad: cv2.error has no stable public base class, re-raised as SourceError
        raise SourceError(f"{origin}: cannot decode image ({exc})") from exc
    if img is None:
        raise SourceError(f"{origin}: data is not a decodable image")
    return as_bgr(img, origin=origin)
