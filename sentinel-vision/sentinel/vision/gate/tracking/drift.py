"""Slow-drift detection: cumulative change against the last judged frame."""

from __future__ import annotations

import numpy as np

from ..config import GateConfig
from ..geometry import BBox, Roi, bbox_from_mask, bbox_iou, roi_slices
from ..ops import ImageOps
from ..scoring import FrameScorer
from ..types import GateState, Reference

__all__ = ["DriftDetector"]

_MIN_OVERLAP = 0.2  # IoU between consecutive drift boxes for the same slow event


class DriftDetector:
    """Finds gradual changes that the fast EMA background absorbs frame by frame.

    A second, slow reference (the last judged frame) is compared with the current frame at a
    low rate and a lower threshold. The comparison uses the same lighting- and shake-tolerant
    structural score as the fast path, so exposure drift and shake cancel out; only new
    structure accumulates. All state lives in ``GateState``.
    """

    def __init__(self, scorer: FrameScorer, ops: ImageOps) -> None:
        """Create a detector sharing the gate's scorer and filter backend."""
        self._scorer = scorer
        self._ops = ops

    def refresh(self, st: GateState, cur: Reference, now: float) -> None:
        """Make ``cur`` the slow reference (a frame was judged or the reference is stale)."""
        st.slow_ref = cur
        st.slow_ts = now
        st.drift_streak = 0
        st.drift_bbox = None

    def check(
        self,
        st: GateState,
        cur: Reference,
        thr_map: np.ndarray,
        roi: Roi | None,
        cfg: GateConfig,
        now: float,
        frame_shape: tuple[int, int],
    ) -> tuple[float, BBox] | None:
        """Compare with the slow reference; return ``(ratio, bbox)`` once drift is confirmed."""
        if not cfg.drift_enabled or st.slow_ref is None or not self._due(st, cfg, now):
            return None
        if st.slow_ts is None:
            st.slow_ts = now
        if now - st.slow_ts >= cfg.drift_refresh_seconds or now < st.slow_ts:
            self.refresh(st, cur, now)
            return None
        mask = self._mask(st.slow_ref, cur, thr_map, cfg)
        if float(mask.mean()) > cfg.global_change_max:  # lighting or scene cut, not drift
            self.refresh(st, cur, now)
            return None
        return self._confirm(st, cur, mask, roi, cfg, now, frame_shape)

    @staticmethod
    def _due(st: GateState, cfg: GateConfig, now: float) -> bool:
        last = st.drift_checked
        if last is not None and last <= now < last + cfg.drift_check_seconds:
            return False
        st.drift_checked = now
        return True

    def _mask(
        self, ref: Reference, cur: Reference, thr_map: np.ndarray, cfg: GateConfig
    ) -> np.ndarray:
        score, dissim = self._scorer.score(cur, [ref], cfg)
        factor = np.float32(cfg.drift_threshold_factor)
        raw = (score > thr_map * factor) & (dissim > cfg.min_dissimilarity)
        return self._ops.open(raw, cfg.open_iterations)

    def _confirm(
        self,
        st: GateState,
        cur: Reference,
        mask: np.ndarray,
        roi: Roi | None,
        cfg: GateConfig,
        now: float,
        frame_shape: tuple[int, int],
    ) -> tuple[float, BBox] | None:
        height, width = mask.shape
        rows, cols = roi_slices(roi, width, height)
        roi_mask = mask[rows, cols]
        ratio = float(roi_mask.mean()) if roi_mask.size else 0.0
        if ratio > cfg.drift_max_area_ratio:  # lighting, not an object
            self.refresh(st, cur, now)
            return None
        if ratio < cfg.drift_min_area_ratio or not roi_mask.any():
            st.drift_streak, st.drift_bbox = 0, None
            return None
        box = bbox_from_mask(roi_mask, rows.start, cols.start, (width, height), (height, width))
        if not self._coherent(st, roi_mask, box, cfg):
            st.drift_streak, st.drift_bbox = 1, box
            return None
        st.drift_streak += 1
        st.drift_bbox = box
        if st.drift_streak < cfg.drift_confirm_checks:
            return None
        bbox = bbox_from_mask(roi_mask, rows.start, cols.start, (width, height), frame_shape)
        self.refresh(st, cur, now)
        return ratio, bbox

    @staticmethod
    def _coherent(st: GateState, roi_mask: np.ndarray, box: BBox, cfg: GateConfig) -> bool:
        """Compact, and in the same place as at the previous comparison (not a moving shadow)."""
        fill = float(roi_mask.sum()) / max(1, box[2] * box[3])
        if fill < cfg.drift_min_fill:
            return False
        return st.drift_bbox is None or bbox_iou(st.drift_bbox, box) >= _MIN_OVERLAP
