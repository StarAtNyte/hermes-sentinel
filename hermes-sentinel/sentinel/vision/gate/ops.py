"""Image primitives with an OpenCV fast path and an equivalent numpy fallback."""

from __future__ import annotations

import logging
import math

import numpy as np

try:  # OpenCV is optional; numpy covers every operation.
    import cv2 as _cv2
except (ImportError, OSError, AttributeError) as exc:  # pragma: no cover - environment
    logging.getLogger(__name__).debug("sentinel: OpenCV unavailable for the gate: %s", exc)
    _cv2 = None

__all__ = ["ImageOps", "cv2_available"]

# Luma weights for BGR input (ITU-R BT.601), matching cv2.COLOR_BGR2GRAY.
_LUMA_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float32)


def cv2_available() -> bool:
    """True when OpenCV imported successfully (the gate then uses it for speed)."""
    return _cv2 is not None


class ImageOps:
    """Image primitives with an OpenCV fast path and an equivalent numpy fallback.

    Borders are reflected without repeating the edge pixel (numpy ``"reflect"`` ==
    OpenCV ``BORDER_REFLECT_101``) in both paths.
    """

    def __init__(self, use_cv2: bool) -> None:
        """Use OpenCV when ``use_cv2`` is true and it is importable, else numpy."""
        self.use_cv2 = bool(use_cv2 and _cv2 is not None)
        self._resize_cache: dict[tuple[int, int], np.ndarray] = {}

    # -- colour / resize ----------------------------------------------------

    @staticmethod
    def to_gray(frame: np.ndarray) -> np.ndarray:
        """HxW, HxWx1, HxWx3 (BGR) or HxWx4 (BGRA) -> float32 HxW."""
        if frame.ndim == 2:
            gray = frame.astype(np.float32, copy=False)
        elif frame.ndim == 3 and frame.shape[2] == 1:
            gray = frame[..., 0].astype(np.float32, copy=False)
        elif frame.ndim == 3 and frame.shape[2] in (3, 4):
            gray = frame[..., :3].astype(np.float32, copy=False) @ _LUMA_BGR
        else:
            raise ValueError(f"unsupported frame shape {frame.shape}")
        if not np.all(np.isfinite(gray)):
            gray = np.nan_to_num(gray, nan=0.0, posinf=255.0, neginf=0.0)
        return np.ascontiguousarray(gray, dtype=np.float32)

    def _area_matrix(self, n_in: int, n_out: int) -> np.ndarray:
        """(n_out, n_in) matrix averaging input cells into output cells by overlap area."""
        key = (n_in, n_out)
        mat = self._resize_cache.get(key)
        if mat is None:
            scale = n_in / n_out
            edges = np.arange(n_out + 1, dtype=np.float64) * scale
            lo, hi = edges[:-1, None], edges[1:, None]
            cells = np.arange(n_in, dtype=np.float64)[None, :]
            overlap = np.clip(np.minimum(hi, cells + 1) - np.maximum(lo, cells), 0.0, None)
            mat = (overlap / scale).astype(np.float32)
            if len(self._resize_cache) > 16:  # bounded: sources rarely change size
                self._resize_cache.clear()
            self._resize_cache[key] = mat
        return mat

    def downscale(self, gray: np.ndarray, max_width: int) -> np.ndarray:
        """Area-downscale so width <= max_width, keeping aspect ratio. Never upscales."""
        h, w = gray.shape
        if w <= max_width:
            return gray
        out_w = max_width
        out_h = max(1, int(round(h * out_w / w)))
        if self.use_cv2:
            return _cv2.resize(gray, (out_w, out_h), interpolation=_cv2.INTER_AREA)
        ry = self._area_matrix(h, out_h)
        rx = self._area_matrix(w, out_w)
        return (ry @ gray @ rx.T).astype(np.float32)

    # -- filters --------------------------------------------------------------

    @staticmethod
    def gaussian_kernel(sigma: float) -> np.ndarray:
        """Normalised 1-D Gaussian kernel with radius ``ceil(3 * sigma)``."""
        radius = max(1, int(math.ceil(3.0 * sigma)))
        xs = np.arange(-radius, radius + 1, dtype=np.float64)
        k = np.exp(-0.5 * (xs / sigma) ** 2)
        return (k / k.sum()).astype(np.float32)

    def blur(self, img: np.ndarray, sigma: float) -> np.ndarray:
        """Separable Gaussian blur (reflect-101 borders)."""
        if sigma <= 0:
            return img
        k = self.gaussian_kernel(sigma)
        r = k.size // 2
        if min(img.shape) <= r:
            return img  # too small to blur meaningfully
        if self.use_cv2:
            return _cv2.sepFilter2D(img, -1, k, k, borderType=_cv2.BORDER_REFLECT_101)
        padded = np.pad(img, r, mode="reflect")
        tmp = np.zeros((padded.shape[0], img.shape[1]), dtype=np.float32)
        for i, wgt in enumerate(k):
            tmp += wgt * padded[:, i : i + img.shape[1]]
        out = np.zeros(img.shape, dtype=np.float32)
        for i, wgt in enumerate(k):
            out += wgt * tmp[i : i + img.shape[0], :]
        return out

    def box_mean(self, img: np.ndarray, size: int) -> np.ndarray:
        """Mean over a ``size x size`` window centred on each pixel (reflect-101 borders)."""
        if self.use_cv2:
            return _cv2.boxFilter(
                img, -1, (size, size), normalize=True, borderType=_cv2.BORDER_REFLECT_101
            )
        r = size // 2
        mode = "reflect" if min(img.shape) > r else "edge"
        padded = np.pad(img.astype(np.float64), r, mode=mode)
        c = np.cumsum(np.cumsum(padded, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        h, w = img.shape
        s = c[size : size + h, size : size + w] - c[:h, size : size + w]
        s = s - c[size : size + h, :w] + c[:h, :w]
        return (s / (size * size)).astype(np.float32)

    @staticmethod
    def _erode(mask: np.ndarray) -> np.ndarray:
        p = np.pad(mask, 1, mode="constant", constant_values=True)
        h, w = mask.shape
        out = np.ones_like(mask)
        for dy in range(3):
            for dx in range(3):
                out &= p[dy : dy + h, dx : dx + w]
        return out

    @staticmethod
    def _dilate(mask: np.ndarray) -> np.ndarray:
        p = np.pad(mask, 1, mode="constant", constant_values=False)
        h, w = mask.shape
        out = np.zeros_like(mask)
        for dy in range(3):
            for dx in range(3):
                out |= p[dy : dy + h, dx : dx + w]
        return out

    def open(self, mask: np.ndarray, iterations: int) -> np.ndarray:
        """Morphological opening with a 3x3 square, ``iterations`` rounds."""
        if iterations <= 0 or not mask.any():
            return mask
        if self.use_cv2:
            m = mask.astype(np.uint8)
            kernel = np.ones((3, 3), np.uint8)
            out = _cv2.morphologyEx(
                m,
                _cv2.MORPH_OPEN,
                kernel,
                iterations=iterations,
                borderType=_cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            return out.astype(bool)
        out = mask
        for _ in range(iterations):
            out = self._erode(out)
        for _ in range(iterations):
            out = self._dilate(out)
        return out
