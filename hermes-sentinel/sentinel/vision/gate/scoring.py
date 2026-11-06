"""Per-frame scoring for the change gate: structural difference, noise floor and baselines."""

from __future__ import annotations

import numpy as np

from .config import GateConfig
from .motion import estimate_shift, noise_bins, shifted, zero_border
from .ops import ImageOps
from .types import GateState, Reference

__all__ = ["FrameScorer"]

_EPS = 1e-6
_NOISE_CAP = 4.0  # a frame's noise estimate may exceed the running level by at most this
_NOISE_ALPHA = np.float32(0.1)  # EMA weight of the running per-bin noise level
_BACKGROUND_FRAMES_CAP = 1_000_000


class FrameScorer:
    """Stateless scoring maths over an :class:`ImageOps`; all state lives in ``GateState``."""

    def __init__(self, ops: ImageOps) -> None:
        """Create a scorer that runs its filters through ``ops``."""
        self._ops = ops

    def reference(self, img: np.ndarray, window: int) -> Reference:
        """A reference image with its local mean and variance over ``window`` pixels."""
        mean = self._ops.box_mean(img, window)
        var = np.maximum(self._ops.box_mean(img * img, window) - mean * mean, 0.0)
        return Reference(img=img, mean=mean, var=var)

    def score(
        self, cur: Reference, refs: list[Reference], cfg: GateConfig
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel unexplained structure, minimised over references and shifts.

        Returns ``(score, dissimilarity)``: ``score`` is the variance the best local affine
        fit cannot explain, ``dissimilarity`` is ``1 - rho^2`` (scale-free, in ``[0, 1]``).
        Each reference is first aligned by its global (phase-correlation) shift, then
        ``shift_tolerance`` pixels around that are searched to absorb residual shake.
        """
        tol, size = cfg.shift_tolerance, cfg.window
        best: np.ndarray | None = None
        best_d: np.ndarray | None = None
        for ref in refs:
            gdx, gdy = estimate_shift(ref.img, cur.img, cfg.max_global_shift)
            for dx, dy in self._candidates(gdx, gdy, tol):
                score, dis = self._shift_score(cur, ref, dx, dy, size)
                if best is None or best_d is None:
                    best, best_d = score, dis
                else:
                    np.minimum(best, score, out=best)
                    np.minimum(best_d, dis, out=best_d)
            # Near the frame edge the aligned reference is padding, not scene: unjudgeable.
            zero_border(best, abs(gdx) + tol + size // 2, abs(gdy) + tol + size // 2)
        assert best is not None and best_d is not None
        return best, best_d

    @staticmethod
    def _candidates(gdx: int, gdy: int, tol: int) -> list[tuple[int, int]]:
        # Search around the estimate and around zero: the estimate can be wrong on
        # flat or very noisy frames, and a static camera is the common case.
        return sorted(
            {
                (cx + dx, cy + dy)
                for cx, cy in {(0, 0), (gdx, gdy)}
                for dy in range(-tol, tol + 1)
                for dx in range(-tol, tol + 1)
            }
        )

    def _shift_score(
        self, cur: Reference, ref: Reference, dx: int, dy: int, size: int
    ) -> tuple[np.ndarray, np.ndarray]:
        r_img = shifted(ref.img, dx, dy)
        r_mean = shifted(ref.mean, dx, dy)
        r_var = shifted(ref.var, dx, dy)
        cov = self._ops.box_mean(cur.img * r_img, size) - cur.mean * r_mean
        denom = np.sqrt(cur.var * r_var) + np.float32(_EPS)
        rho = np.clip(cov / denom, 0.0, 1.0)
        dis = 1.0 - rho * rho
        return np.maximum(cur.var, r_var) * dis, dis

    @staticmethod
    def thresholds(
        score: np.ndarray,
        brightness: np.ndarray,
        cfg: GateConfig,
        noise_ref: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel score threshold map and the updated running noise level."""
        levels, bin_idx = noise_bins(score, brightness, cfg.noise_bins)
        if noise_ref is not None and noise_ref.shape == levels.shape:
            # A change covering much of the frame inflates this frame's medians; cap them
            # by the recent noise history so a scene cut still reads as a global change.
            levels = np.minimum(levels, np.float32(_NOISE_CAP) * noise_ref)
            noise_ref = (1 - _NOISE_ALPHA) * noise_ref + _NOISE_ALPHA * levels
        else:
            noise_ref = levels.copy()
        noise = levels[bin_idx].reshape(score.shape)
        thr_map = np.maximum(np.float32(cfg.min_score), np.float32(cfg.noise_k) * noise)
        return thr_map, noise_ref

    @staticmethod
    def rebaseline(st: GateState, img: np.ndarray, cur: Reference, shape: tuple[int, int]) -> None:
        """Restart the references from this frame."""
        st.shape = shape
        st.since_baseline = 1
        st.noise_ref = None
        st.prev = cur
        st.background = img.copy()
        st.background_ref = cur

    def update_background(
        self, st: GateState, img: np.ndarray, cur: Reference, cfg: GateConfig
    ) -> None:
        """Blend ``img`` into the running background and advance the previous frame."""
        # Running mean until the EMA horizon is reached, so a fresh background is averaged
        # over every frame seen so far instead of being one noisy frame for a long time.
        st.since_baseline = min(st.since_baseline + 1, _BACKGROUND_FRAMES_CAP)
        a = np.float32(max(cfg.background_alpha, 1.0 / st.since_baseline))
        assert st.background is not None
        st.background = (1.0 - a) * st.background + a * img
        st.background_ref = self.reference(st.background, cfg.window)
        st.prev = cur
