"""Cheap local change gate, run on every frame before any model call.

The gate decides whether a frame is worth sending to the vision judge. It must
fire on real scene changes (an object appears or disappears, text on a screen
updates) and stay quiet under the nuisances real cameras produce: sensor noise,
lighting flicker, exposure drift, IR night-mode switches, moving shadows,
headlight sweeps, codec block artifacts and small camera shake.

Pipeline (per frame)::

    BGR frame -> grayscale -> area downscale to <= max_width -> Gaussian blur
      -> brightness normalise (divide by frame mean)
      -> local structural difference against two references
         (the previous frame and a running EMA background), tolerant to small
         shifts (camera shake) and to local gain/offset (lighting)
      -> adaptive threshold (multiple of the frame's robust noise level)
      -> morphological open -> changed-area ratio inside the ROI

The structural difference is the core of the nuisance rejection. For every
pixel it fits ``current ~= a * reference + b`` over a small window and keeps
the variance the fit cannot explain::

    score = max(var_cur, var_ref) * (1 - max(rho, 0) ** 2)

where ``rho`` is the windowed correlation. Any lighting change that is smooth
at the window scale (global gain, drift, shadow, headlight, IR greyscale
curve) is absorbed by ``a`` and ``b``; new structure (an object edge, a
different digit) is not. Taking the minimum over a few pixel shifts of the
reference absorbs camera shake. A pixel only counts as changed when it is new
relative to *both* references, so a one-frame glitch that recovers is ignored
and a persistent change fires on its onset, then goes quiet.

Because the previous frame is one of the references, a change that creeps in more
slowly than the noise floor per frame is not reported as motion; the periodic tick
(below) is what catches slow state changes.

If more than ``global_change_max`` of the frame changes at once (camera bumped,
scene cut), the gate re-baselines instead of reporting a change.

The gate also emits a periodic ``tick`` every ``idle_check_seconds`` so state
rules ("the stove is on", "the package is gone") are re-judged without motion.

numpy is the only hard dependency. When OpenCV is importable the heavy
filters run through it (same kernels, same border handling); results match the
numpy path to floating-point tolerance.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import numpy as np

try:  # OpenCV is optional; numpy covers every operation.
    import cv2 as _cv2
except Exception:  # pragma: no cover - depends on the environment
    _cv2 = None

logger = logging.getLogger(__name__)

__all__ = [
    "BBox",
    "Roi",
    "GateConfig",
    "GateResult",
    "ChangeGate",
    "normalize_roi",
    "roi_from_pixels",
    "bbox_iou",
    "cv2_available",
]

BBox = tuple[int, int, int, int]
"""``(x, y, w, h)`` in pixels of the frame given to :meth:`ChangeGate.process`."""

Roi = tuple[float, float, float, float]
"""``(x, y, w, h)`` as fractions of the frame width/height, each in ``[0, 1]``."""

Backend = Literal["auto", "numpy", "cv2"]

# Luma weights for BGR input (ITU-R BT.601), matching cv2.COLOR_BGR2GRAY.
_LUMA_BGR = np.array([0.114, 0.587, 0.299], dtype=np.float32)
_NORM_LEVEL = 128.0  # frames are rescaled so their mean sits here
_EPS = 1e-6
_NOISE_CAP = 4.0  # a frame's noise estimate may exceed the running level by at most this
_NOISE_ALPHA = np.float32(0.1)  # EMA weight of the running per-bin noise level
_SHIFT_PEAK_SIGMA = 6.0  # phase-correlation peak must stand this many std above the surface


def cv2_available() -> bool:
    """True when OpenCV imported successfully (the gate then uses it for speed)."""
    return _cv2 is not None


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class GateConfig:
    """Tunable gate parameters. Defaults are tuned on ``tests/harness/scenes.py``.

    Attributes:
        max_width: frames wider than this are area-downscaled to it (never upscaled).
        blur_sigma: Gaussian pre-blur in processed pixels; suppresses noise and codec blocks.
        window: side of the square window for local statistics (odd, >= 3).
        shift_tolerance: max |dx|, |dy| in processed pixels searched around the global
            alignment to absorb residual shake and small rotation.
        max_global_shift: largest whole-frame translation (processed pixels) compensated as
            camera shake via phase correlation; 0 disables global alignment.
        background_alpha: EMA weight of each new frame in the running background.
        noise_k: a pixel is changed when its score exceeds ``noise_k`` times the
            median score of pixels with similar brightness (robust noise estimate) ...
        min_score: ... and also exceeds this absolute floor (variance units, grey levels^2
            after normalisation to mean 128). Keeps clean screens from triggering on rounding.
        noise_bins: brightness bins for the signal-dependent noise estimate (1 = global).
        min_dissimilarity: ... and its scale-free dissimilarity ``1 - rho^2`` exceeds this.
            Rejects strong edges that merely re-sample differently (sub-pixel shake).
        min_area_ratio: fraction of the ROI that must change for ``changed=True``.
        global_change_max: if more than this fraction of the whole frame changes, treat it as
            lighting/shake/scene cut: re-baseline and report not changed.
        open_iterations: rounds of 3x3 morphological opening on the change mask.
        idle_check_seconds: emit ``tick`` after this long without a change or tick (<= 0
            disables ticks).
        tick_on_start: emit a tick on the first frame so state rules get an initial verdict.
    """

    max_width: int = 320
    blur_sigma: float = 1.5
    window: int = 7
    shift_tolerance: int = 1
    max_global_shift: int = 8
    background_alpha: float = 0.05
    noise_k: float = 9.0
    min_score: float = 40.0
    min_dissimilarity: float = 0.25
    noise_bins: int = 8
    min_area_ratio: float = 0.002
    global_change_max: float = 0.6
    open_iterations: int = 1
    idle_check_seconds: float = 300.0
    tick_on_start: bool = True

    def __post_init__(self) -> None:
        checks: list[tuple[bool, str]] = [
            (self.max_width >= 16, "max_width must be >= 16"),
            (0.0 <= self.blur_sigma <= 10.0, "blur_sigma must be in [0, 10]"),
            (self.window >= 3 and self.window % 2 == 1, "window must be odd and >= 3"),
            (0 <= self.shift_tolerance <= 4, "shift_tolerance must be in [0, 4]"),
            (0 <= self.max_global_shift <= 64, "max_global_shift must be in [0, 64]"),
            (1 <= self.noise_bins <= 64, "noise_bins must be in [1, 64]"),
            (0.0 <= self.min_dissimilarity < 1.0, "min_dissimilarity must be in [0, 1)"),
            (0.0 < self.background_alpha <= 1.0, "background_alpha must be in (0, 1]"),
            (self.noise_k > 0.0, "noise_k must be > 0"),
            (self.min_score >= 0.0, "min_score must be >= 0"),
            (0.0 <= self.min_area_ratio < 1.0, "min_area_ratio must be in [0, 1)"),
            (0.0 < self.global_change_max <= 1.0, "global_change_max must be in (0, 1]"),
            (0 <= self.open_iterations <= 5, "open_iterations must be in [0, 5]"),
            (math.isfinite(self.idle_check_seconds), "idle_check_seconds must be finite"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> GateConfig:
        """Build a config from a plain dict (e.g. plugin config), ignoring unknown keys.

        Raises ``ValueError`` for values of the wrong type or out of range.
        """
        if not data:
            return cls()
        kwargs: dict[str, Any] = {}
        for f in cls.__dataclass_fields__.values():
            if f.name not in data or data[f.name] is None:
                continue
            raw = data[f.name]
            try:
                if f.type in ("bool", bool):
                    if not isinstance(raw, bool):
                        raise TypeError
                    kwargs[f.name] = raw
                elif f.type in ("int", int):
                    if isinstance(raw, bool) or float(raw) != int(raw):
                        raise TypeError
                    kwargs[f.name] = int(raw)
                else:
                    if isinstance(raw, bool):
                        raise TypeError
                    kwargs[f.name] = float(raw)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"invalid value for {f.name}: {raw!r}") from None
        return cls(**kwargs)


# --------------------------------------------------------------------- result


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


def _roi_slices(roi: Roi | None, width: int, height: int) -> tuple[slice, slice]:
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


# ------------------------------------------------------------- image kernels


class _Ops:
    """Image primitives with an OpenCV fast path and an equivalent numpy fallback.

    Borders are reflected without repeating the edge pixel (numpy ``"reflect"`` ==
    OpenCV ``BORDER_REFLECT_101``) in both paths.
    """

    def __init__(self, use_cv2: bool) -> None:
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


def _hann(h: int, w: int) -> np.ndarray:
    return (np.hanning(h)[:, None] * np.hanning(w)[None, :]).astype(np.float32)


def estimate_shift(ref: np.ndarray, cur: np.ndarray, max_shift: int) -> tuple[int, int]:
    """Integer global translation ``(dx, dy)`` with ``_shifted(ref, dx, dy) ~= cur``.

    Phase correlation (numpy FFT) on Hann-windowed, mean-removed images. Shifts larger
    than ``max_shift`` in either axis are rejected (returns ``(0, 0)``): a jump that big
    is a scene change, not shake.
    """
    if max_shift <= 0 or ref.shape != cur.shape or min(ref.shape) < 8:
        return (0, 0)
    win = _hann(*ref.shape)
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


def _noise_bins(
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


def _noise_level(score: np.ndarray, brightness: np.ndarray, bins: int) -> np.ndarray:
    """Per-pixel noise level map (see :func:`_noise_bins`)."""
    levels, idx = _noise_bins(score, brightness, bins)
    return levels[idx].reshape(score.shape)


def _zero_border(img: np.ndarray, bx: int, by: int) -> None:
    """Zero a band ``bx`` columns / ``by`` rows wide along every edge, in place."""
    h, w = img.shape
    bx, by = min(bx, w // 2), min(by, h // 2)
    if by > 0:
        img[:by] = 0
        img[h - by :] = 0
    if bx > 0:
        img[:, :bx] = 0
        img[:, w - bx :] = 0


def _shifted(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Translate by (dx, dy) pixels with edge replication (content moves right/down)."""
    if dx == 0 and dy == 0:
        return img
    m = max(abs(dx), abs(dy))
    h, w = img.shape
    padded = np.pad(img, m, mode="edge")
    return padded[m - dy : m - dy + h, m - dx : m - dx + w]


@dataclass
class _Reference:
    """A reference image plus its cached local statistics."""

    img: np.ndarray
    mean: np.ndarray
    var: np.ndarray


# ----------------------------------------------------------------------- gate


@dataclass
class _State:
    shape: tuple[int, int] | None = None  # input (h, w)
    prev: _Reference | None = None
    background: np.ndarray | None = None
    background_ref: _Reference | None = None
    last_tick: float | None = None
    noise_ref: np.ndarray | None = None  # running per-brightness-bin noise level
    since_baseline: int = 0  # frames blended into the background since the last baseline
    stats: dict[str, int] = field(
        default_factory=lambda: {"frames": 0, "changed": 0, "ticks": 0, "rebaselines": 0}
    )


class ChangeGate:
    """Per-source change detector with ROI and periodic idle ticks. Thread safe.

    Example::

        gate = ChangeGate(GateConfig(idle_check_seconds=300), roi=(0.5, 0.5, 0.5, 0.5))
        for ts, frame in source:
            result = gate.process(frame, ts)
            if result.should_judge:
                judge(frame, result)
    """

    def __init__(
        self,
        config: GateConfig | None = None,
        *,
        roi: Sequence[float] | None = None,
        backend: Backend = "auto",
        clock: Any = time.monotonic,
    ) -> None:
        """Create a gate.

        Args:
            config: tuning parameters (defaults when None).
            roi: optional fractional ``(x, y, w, h)`` region; changes outside it are ignored.
            backend: ``"auto"`` (OpenCV if importable), ``"numpy"`` or ``"cv2"``.
            clock: zero-arg callable returning seconds, used when ``process`` gets no ``ts``.
        """
        if backend not in ("auto", "numpy", "cv2"):
            raise ValueError(f"backend must be 'auto', 'numpy' or 'cv2', got {backend!r}")
        if backend == "cv2" and _cv2 is None:
            raise RuntimeError("backend='cv2' requested but OpenCV is not installed")
        self._config = config or GateConfig()
        self._roi = normalize_roi(roi)
        self._ops = _Ops(use_cv2=backend != "numpy")
        self._clock = clock
        self._lock = threading.Lock()
        self._state = _State()

    # -- properties -----------------------------------------------------------

    @property
    def config(self) -> GateConfig:
        return self._config

    @property
    def roi(self) -> Roi | None:
        return self._roi

    @property
    def backend(self) -> str:
        return "cv2" if self._ops.use_cv2 else "numpy"

    def stats(self) -> dict[str, int]:
        """Counters since creation: frames, changed, ticks, rebaselines."""
        with self._lock:
            return dict(self._state.stats)

    # -- control --------------------------------------------------------------

    def set_roi(self, roi: Sequence[float] | None) -> None:
        """Change the ROI (fractions). The background is kept: it covers the whole frame."""
        new = normalize_roi(roi)
        with self._lock:
            self._roi = new

    def update_config(self, **changes: Any) -> None:
        """Replace config fields (validated). Resets the baseline if geometry changes."""
        with self._lock:
            new = replace(self._config, **changes)
            geometry = ("max_width", "blur_sigma", "window")
            if any(getattr(new, k) != getattr(self._config, k) for k in geometry):
                self._reset_locked(keep_tick=True)
            self._config = new

    def reset(self) -> None:
        """Forget the background; the next frame becomes the new baseline (and ticks)."""
        with self._lock:
            self._reset_locked(keep_tick=False)

    def _reset_locked(self, *, keep_tick: bool) -> None:
        st = self._state
        st.shape = None
        st.prev = None
        st.background = None
        st.background_ref = None
        st.noise_ref = None
        if not keep_tick:
            st.last_tick = None

    # -- main entry -----------------------------------------------------------

    def process(self, frame: np.ndarray, ts: float | None = None) -> GateResult:
        """Feed one frame; return whether it changed and whether a tick is due.

        Args:
            frame: ``HxWx3`` BGR (also accepts HxW grey, HxWx1, HxWx4 BGRA), any real dtype.
            ts: frame timestamp in seconds (monotonic preferred); defaults to ``clock()``.

        Raises:
            ValueError: the frame is not a non-empty 2-D/3-D numeric array.
        """
        arr = self._validate(frame)
        now = float(self._clock() if ts is None else ts)
        if not math.isfinite(now):
            raise ValueError(f"timestamp must be finite, got {ts!r}")
        with self._lock:
            result = self._process_locked(arr, now)
            st = self._state
            st.stats["frames"] += 1
            st.stats["changed"] += int(result.changed)
            st.stats["ticks"] += int(result.tick)
            st.stats["rebaselines"] += int(result.rebaselined)
            return result

    @staticmethod
    def _validate(frame: Any) -> np.ndarray:
        if not isinstance(frame, np.ndarray):
            raise ValueError(f"frame must be a numpy array, got {type(frame).__name__}")
        if frame.ndim not in (2, 3) or frame.size == 0 or min(frame.shape[:2]) < 2:
            raise ValueError(f"frame must be a non-empty HxW[xC] image, got shape {frame.shape}")
        if frame.ndim == 3 and frame.shape[2] not in (1, 3, 4):
            raise ValueError(f"frame must have 1, 3 or 4 channels, got {frame.shape[2]}")
        if not (np.issubdtype(frame.dtype, np.integer) or np.issubdtype(frame.dtype, np.floating)):
            raise ValueError(f"frame dtype must be numeric, got {frame.dtype}")
        return frame

    def _tick_due(self, now: float) -> bool:
        st, cfg = self._state, self._config
        if cfg.idle_check_seconds <= 0:
            return False
        if st.last_tick is None:
            if cfg.tick_on_start:
                return True
            st.last_tick = now
            return False
        if now < st.last_tick:  # clock went backwards (source restart): restart the period
            st.last_tick = now
            return False
        return now - st.last_tick >= cfg.idle_check_seconds

    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        cfg, ops = self._config, self._ops
        gray = ops.to_gray(frame)
        if np.issubdtype(frame.dtype, np.integer) and frame.dtype.itemsize > 1:
            # 16-bit sources: scale into the 8-bit range the thresholds assume.
            gray = gray * np.float32(255.0 / np.iinfo(frame.dtype).max)
        small = ops.downscale(gray, cfg.max_width)
        small = ops.blur(small, cfg.blur_sigma)
        mean = float(small.mean())
        # Brightness normalisation: rejects global flicker/drift before any comparison.
        scale = _NORM_LEVEL / mean if mean > 1.0 else 1.0
        return np.ascontiguousarray(small * np.float32(scale), dtype=np.float32)

    def _reference(self, img: np.ndarray) -> _Reference:
        size = self._config.window
        mean = self._ops.box_mean(img, size)
        var = np.maximum(self._ops.box_mean(img * img, size) - mean * mean, 0.0)
        return _Reference(img=img, mean=mean, var=var)

    def _score(self, cur: _Reference, refs: list[_Reference]) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel unexplained structure, minimised over references and shifts.

        Returns ``(score, dissimilarity)``: ``score`` is the variance the best local affine
        fit cannot explain, ``dissimilarity`` is ``1 - rho^2`` (scale-free, in ``[0, 1]``).
        Each reference is first aligned by its global (phase-correlation) shift, then
        ``shift_tolerance`` pixels around that are searched to absorb residual shake.
        """
        cfg, ops = self._config, self._ops
        size, tol = cfg.window, cfg.shift_tolerance
        best: np.ndarray | None = None
        best_d: np.ndarray | None = None
        for ref in refs:
            gdx, gdy = estimate_shift(ref.img, cur.img, cfg.max_global_shift)
            # Search around the estimate and around zero: the estimate can be wrong on
            # flat or very noisy frames, and a static camera is the common case.
            candidates = {
                (cx + dx, cy + dy)
                for cx, cy in {(0, 0), (gdx, gdy)}
                for dy in range(-tol, tol + 1)
                for dx in range(-tol, tol + 1)
            }
            for dx, dy in sorted(candidates):
                r_img = _shifted(ref.img, dx, dy)
                r_mean = _shifted(ref.mean, dx, dy)
                r_var = _shifted(ref.var, dx, dy)
                cov = ops.box_mean(cur.img * r_img, size) - cur.mean * r_mean
                denom = np.sqrt(cur.var * r_var) + np.float32(_EPS)
                rho = np.clip(cov / denom, 0.0, 1.0)
                dis = 1.0 - rho * rho
                score = np.maximum(cur.var, r_var) * dis
                if best is None or best_d is None:
                    best, best_d = score, dis
                else:
                    np.minimum(best, score, out=best)
                    np.minimum(best_d, dis, out=best_d)
            # Near the frame edge the aligned reference is padding, not scene: unjudgeable.
            _zero_border(best, abs(gdx) + tol + size // 2, abs(gdy) + tol + size // 2)
        assert best is not None and best_d is not None
        return best, best_d

    def _process_locked(self, frame: np.ndarray, now: float) -> GateResult:
        cfg, st = self._config, self._state
        tick = self._tick_due(now)
        if tick:
            st.last_tick = now

        img = self._preprocess(frame)
        cur = self._reference(img)
        shape = frame.shape[:2]
        if st.prev is None or st.background is None or st.shape != shape:
            reason = "warmup" if st.shape is None else "resized"
            self._rebaseline(img, cur, shape)
            return GateResult(False, 0.0, None, tick=tick, rebaselined=True, reason=reason)

        assert st.background_ref is not None
        score, dissim = self._score(cur, [st.prev, st.background_ref])
        levels, bin_idx = _noise_bins(score, cur.mean, cfg.noise_bins)
        if st.noise_ref is not None and st.noise_ref.shape == levels.shape:
            # A change covering much of the frame inflates this frame's medians; cap them
            # by the recent noise history so a scene cut still reads as a global change.
            levels = np.minimum(levels, np.float32(_NOISE_CAP) * st.noise_ref)
            st.noise_ref = (1 - _NOISE_ALPHA) * st.noise_ref + _NOISE_ALPHA * levels
        else:
            st.noise_ref = levels.copy()
        noise = levels[bin_idx].reshape(score.shape)
        thr_map = np.maximum(np.float32(cfg.min_score), np.float32(cfg.noise_k) * noise)
        thr = float(np.median(thr_map))
        raw = (score > thr_map) & (dissim > cfg.min_dissimilarity)
        mask = self._ops.open(raw, cfg.open_iterations)

        global_ratio = float(mask.mean())
        if global_ratio > cfg.global_change_max:
            logger.debug(
                "gate: global change %.2f > %.2f, re-baselining",
                global_ratio,
                cfg.global_change_max,
            )
            self._rebaseline(img, cur, shape)
            return GateResult(
                False,
                global_ratio,
                None,
                tick=tick,
                rebaselined=True,
                reason="global_change",
                score_threshold=thr,
            )

        ph, pw = img.shape
        rows, cols = _roi_slices(self._roi, pw, ph)
        roi_mask = mask[rows, cols]
        ratio = float(roi_mask.mean()) if roi_mask.size else 0.0
        changed = ratio >= cfg.min_area_ratio and bool(roi_mask.any())
        bbox = None
        if changed:
            bbox = self._bbox(roi_mask, rows.start, cols.start, (pw, ph), shape)
            st.last_tick = now  # a judged change resets the idle timer
        self._update_background(img, cur)
        return GateResult(
            changed,
            ratio,
            bbox,
            tick=tick,
            reason="change" if changed else "quiet",
            score_threshold=thr,
        )

    @staticmethod
    def _bbox(
        mask: np.ndarray,
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

    def _rebaseline(self, img: np.ndarray, cur: _Reference, shape: tuple[int, int]) -> None:
        st = self._state
        st.shape = shape
        st.since_baseline = 1
        st.noise_ref = None
        st.prev = cur
        st.background = img.copy()
        st.background_ref = cur

    def _update_background(self, img: np.ndarray, cur: _Reference) -> None:
        st = self._state
        # Running mean until the EMA horizon is reached, so a fresh background is averaged
        # over every frame seen so far instead of being one noisy frame for a long time.
        st.since_baseline = min(st.since_baseline + 1, 1_000_000)
        a = np.float32(max(self._config.background_alpha, 1.0 / st.since_baseline))
        assert st.background is not None
        st.background = (1.0 - a) * st.background + a * img
        st.background_ref = self._reference(st.background)
        st.prev = cur
