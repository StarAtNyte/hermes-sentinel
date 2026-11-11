"""Re-encode teaching images to the small JPEG size that is re-sent with every check."""

from __future__ import annotations

import logging

from ....vision import encode_jpeg
from .results import ToolError

logger = logging.getLogger(__name__)

__all__ = ["EXAMPLE_JPEG_QUALITY", "EXAMPLE_MAX_SIDE", "decode_to_jpeg", "small_jpeg"]

#: Teaching examples are re-encoded to this longest side (they are re-sent with every check).
EXAMPLE_MAX_SIDE = 768
EXAMPLE_JPEG_QUALITY = 80


def decode_to_jpeg(data: bytes) -> bytes:
    """Any image cv2 can read as a JPEG whose longest side is at most ``EXAMPLE_MAX_SIDE``.

    Without OpenCV only JPEG input is accepted, stored unchanged. Raises :class:`ToolError`.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        logger.debug("sentinel: OpenCV unavailable for example decoding: %s", exc)
        if data.startswith(b"\xff\xd8\xff"):
            return data
        raise ToolError("only JPEG images can be used without OpenCV installed") from None
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ToolError("the file is not an image Sentinel can read")
    jpeg = encode_jpeg(img, max_side=EXAMPLE_MAX_SIDE, quality=EXAMPLE_JPEG_QUALITY)
    if jpeg is None:
        raise ToolError("could not convert the image to JPEG")
    return jpeg


def small_jpeg(data: bytes) -> bytes:
    """``data`` re-encoded to the example size when that works, else unchanged. Never raises."""
    try:
        return decode_to_jpeg(data)
    except ToolError:
        return data
