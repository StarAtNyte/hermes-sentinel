"""The change gate: a cheap, thread-safe per-source detector run before any model call."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Literal

import numpy as np

from . import ops as _ops
from .config import GateConfig
from .frames import preprocess_frame, validate_frame
from .geometry import Roi, bbox_from_mask, normalize_roi, roi_slices
from .ops import ImageOps
from .scoring import FrameScorer
from .tracking import DriftDetector, RecentFrames, TimedFrame
from .types import GateResult, GateState, Reference

__all__ = ["ChangeGate"]

logger = logging.getLogger(__name__)

Backend = Literal["auto", "numpy", "cv2"]


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
        if backend == "cv2" and not _ops.cv2_available():
            raise RuntimeError("backend='cv2' requested but OpenCV is not installed")
        self._config = config or GateConfig()
        self._roi = normalize_roi(roi)
        self._ops = ImageOps(use_cv2=backend != "numpy")
        self._scorer = FrameScorer(self._ops)
        self._drift = DriftDetector(self._scorer, self._ops)
        self._clock = clock
        self._lock = threading.Lock()  # guards _state, _config, _roi and _recent
        self._state = GateState()
        self._recent = RecentFrames()

    # -- properties -----------------------------------------------------------

    @property
    def config(self) -> GateConfig:
        """The active tuning parameters."""
        return self._config

    @property
    def roi(self) -> Roi | None:
        """The active fractional region of interest, or None for the whole frame."""
        return self._roi

    @property
    def backend(self) -> str:
        """``"cv2"`` or ``"numpy"``: the filter implementation in use."""
        return "cv2" if self._ops.use_cv2 else "numpy"

    def stats(self) -> dict[str, int]:
        """Counters since creation: frames, changed, ticks, rebaselines, drifts."""
        with self._lock:
            return dict(self._state.stats)

    def recent_frames(self, count: int | None = None) -> list[TimedFrame]:
        """The newest ``count`` kept frames (all when None), oldest first.

        Frames are ``uint8`` numpy arrays (read-only, width capped by ``recent_max_width``)
        spaced at least ``recent_spacing_seconds`` apart, taken after each ``process`` call,
        so the newest may be the frame just processed. Use ``ts`` to tell.
        """
        with self._lock:
            return self._recent.latest(count)

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
            self._recent.clear()

    def _reset_locked(self, *, keep_tick: bool) -> None:
        st = self._state
        st.shape = None
        st.prev = None
        st.background = None
        st.background_ref = None
        st.noise_ref = None
        st.slow_ref = None
        st.slow_ts = None
        st.drift_checked = None
        st.drift_streak = 0
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
        arr = validate_frame(frame)
        now = float(self._clock() if ts is None else ts)
        if not math.isfinite(now):
            raise ValueError(f"timestamp must be finite, got {ts!r}")
        with self._lock:
            result = self._process_locked(arr, now)
            self._recent.offer(arr, now, self._config)
            st = self._state
            st.stats["drifts"] += int(result.drift)
            st.stats["frames"] += 1
            st.stats["changed"] += int(result.changed)
            st.stats["ticks"] += int(result.tick)
            st.stats["rebaselines"] += int(result.rebaselined)
            return result

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

    def _process_locked(self, frame: np.ndarray, now: float) -> GateResult:
        st = self._state
        tick = self._tick_due(now)
        if tick:
            st.last_tick = now
        img = preprocess_frame(frame, self._ops, self._config)
        cur = self._scorer.reference(img, self._config.window)
        shape = frame.shape[:2]
        if st.prev is None or st.background is None or st.shape != shape:
            reason = "warmup" if st.shape is None else "resized"
            self._scorer.rebaseline(st, img, cur, shape)
            return GateResult(False, 0.0, None, tick=tick, rebaselined=True, reason=reason)
        return self._detect_locked(img, cur, shape, tick, now)

    def _detect_locked(
        self, img: np.ndarray, cur: Reference, shape: tuple[int, int], tick: bool, now: float
    ) -> GateResult:
        cfg, st = self._config, self._state
        assert st.prev is not None and st.background_ref is not None
        score, dissim = self._scorer.score(cur, [st.prev, st.background_ref], cfg)
        thr_map, st.noise_ref = self._scorer.thresholds(score, cur.mean, cfg, st.noise_ref)
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
            self._scorer.rebaseline(st, img, cur, shape)
            return GateResult(
                False,
                global_ratio,
                None,
                tick=tick,
                rebaselined=True,
                reason="global_change",
                score_threshold=thr,
            )
        return self._roi_result(img, cur, mask, thr_map, shape, tick, now)

    def _roi_result(
        self,
        img: np.ndarray,
        cur: Reference,
        mask: np.ndarray,
        thr_map: np.ndarray,
        shape: tuple[int, int],
        tick: bool,
        now: float,
    ) -> GateResult:
        cfg, st = self._config, self._state
        thr = float(np.median(thr_map))
        ph, pw = img.shape
        rows, cols = roi_slices(self._roi, pw, ph)
        roi_mask = mask[rows, cols]
        ratio = float(roi_mask.mean()) if roi_mask.size else 0.0
        changed = ratio >= cfg.min_area_ratio and bool(roi_mask.any())
        bbox = None
        if changed:
            bbox = bbox_from_mask(roi_mask, rows.start, cols.start, (pw, ph), shape)
            st.last_tick = now  # a judged change resets the idle timer
        self._scorer.update_background(st, img, cur, cfg)
        if not changed:
            return self._quiet_result(cur, thr_map, shape, tick, now, ratio, thr)
        self._drift.refresh(st, cur, now)
        return GateResult(True, ratio, bbox, tick=tick, reason="change", score_threshold=thr)

    def _quiet_result(
        self,
        cur: Reference,
        thr_map: np.ndarray,
        shape: tuple[int, int],
        tick: bool,
        now: float,
        ratio: float,
        thr: float,
    ) -> GateResult:
        """Result for a frame without a fast change: a tick, a drift, or plain quiet."""
        cfg, st = self._config, self._state
        if tick:  # the judge will see this frame, so it becomes the slow reference
            self._drift.refresh(st, cur, now)
            return GateResult(False, ratio, None, tick=True, reason="quiet", score_threshold=thr)
        found = self._drift.check(st, cur, thr_map, self._roi, cfg, now, shape)
        if found is None:
            return GateResult(False, ratio, None, reason="quiet", score_threshold=thr)
        st.last_tick = now
        return GateResult(True, found[0], found[1], reason="drift", score_threshold=thr, drift=True)
