"""Global-shift estimation and per-brightness noise statistics for the change gate."""

from __future__ import annotations

import numpy as np

__all__ = ["estimate_shift", "hann", "noise_bins", "noise_level", "shifted", "zero_border"]

_SHIFT_PEAK_SIGMA = 6.0  # phase-correlation peak must stand this many std above the surface


def hann(h: int, w: int) -> np.ndarray:
    """2-D Hann window of shape ``(h, w)`` as float32."""
    return (np.hanning(h)[:, None] * np.hanning(w)[None, :]).astype(np.float32)


def estimate_shift(ref: np.ndarray, cur: np.ndarray, max_shift: int) -> tuple[int, int]:
    """Integer global translation ``(dx, dy)`` with ``shifted(ref, dx, dy) ~= cur``.

    Phase correlation (numpy FFT) on Hann-windowed, mean-removed images. Shifts larger
    than ``max_shift`` in either axis are rejected (returns ``(0, 0)``): a jump that big
    is a scene change, not shake.
    """
    if max_shift <= 0 or ref.shape != cur.shape or min(ref.shape) < 8:
        return (0, 0)
    win = hann(*ref.shape)
    fa = np.fft.rfft2((ref - ref.mean()) * win)
    fb = np.fft.rfft2((cur - cur.mean()) * win)
    cross = fb * np.conj(fa)
    mag = np.abs(cross)
    if not np.any(mag > 0):
        return (0, 0)
    # Partial whitening (|X|^-0.5) keeps the sharp peak of phase correlation while letting
    # strong low-frequency structure outweigh noise-dominated high frequencies.
    cross /= np.sqrt(np.maximum(mag, 1e-9))
    corr = np.fft.irfft2(cross, s=ref.shape)
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    peak = float(corr[iy, ix])
    spread = float(corr.std())
    if not (spread > 0 and peak > _SHIFT_PEAK_SIGMA * spread):
        return (0, 0)  # no clear peak: flat or noise-dominated frame
    h, w = ref.shape
    dy = iy - h if iy > h // 2 else iy
    dx = ix - w if ix > w // 2 else ix
    if abs(dx) > max_shift or abs(dy) > max_shift:
        return (0, 0)
    return (int(dx), int(dy))


def noise_bins(
    score: np.ndarray, brightness: np.ndarray, bins: int
) -> tuple[np.ndarray, np.ndarray]:
    """Robust noise level per brightness bin: ``(levels[bins], bin_index_per_pixel)``.

    Camera noise grows with signal (shot noise), so one global level under-estimates it in
    bright areas. Pixels are split into ``bins`` equal-population brightness bins and each
    bin's median score is its noise level. A real change covers a small fraction of any bin,
    so the medians ignore it.
    """
    flat_s = score.ravel()
    if bins <= 1:
        return (
            np.array([np.median(flat_s)], dtype=np.float32),
            np.zeros(flat_s.shape, dtype=np.intp),
        )
    flat_b = brightness.ravel()
    edges = np.quantile(flat_b, np.linspace(0.0, 1.0, bins + 1)[1:-1])
    idx = np.searchsorted(edges, flat_b, side="right")
    levels = np.empty(bins, dtype=np.float32)
    fallback = float(np.median(flat_s))
    for b in range(bins):
        sel = flat_s[idx == b]
        levels[b] = float(np.median(sel)) if sel.size >= 16 else fallback
    return levels, idx


def noise_level(score: np.ndarray, brightness: np.ndarray, bins: int) -> np.ndarray:
    """Per-pixel noise level map (see :func:`noise_bins`)."""
    levels, idx = noise_bins(score, brightness, bins)
    return levels[idx].reshape(score.shape)


def zero_border(img: np.ndarray, bx: int, by: int) -> None:
    """Zero a band ``bx`` columns / ``by`` rows wide along every edge, in place."""
    h, w = img.shape
    bx, by = min(bx, w // 2), min(by, h // 2)
    if by > 0:
        img[:by] = 0
        img[h - by :] = 0
    if bx > 0:
        img[:, :bx] = 0
        img[:, w - bx :] = 0


def shifted(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Translate by (dx, dy) pixels with edge replication (content moves right/down)."""
    if dx == 0 and dy == 0:
        return img
    m = max(abs(dx), abs(dy))
    h, w = img.shape
    padded = np.pad(img, m, mode="edge")
    return padded[m - dy : m - dy + h, m - dx : m - dx + w]
