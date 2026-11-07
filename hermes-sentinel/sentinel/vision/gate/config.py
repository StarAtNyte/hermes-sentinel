"""Tunable parameters of the change gate."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["GateConfig"]


def _coerce_bool(raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise TypeError
    return raw


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool) or float(raw) != int(raw):
        raise TypeError
    return int(raw)


def _coerce_float(raw: Any) -> float:
    if isinstance(raw, bool):
        raise TypeError
    return float(raw)


_COERCERS: dict[Any, Callable[[Any], Any]] = {
    "bool": _coerce_bool,
    bool: _coerce_bool,
    "int": _coerce_int,
    int: _coerce_int,
}


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
        drift_enabled: also compare against a slow reference (last judged frame) so gradual
            changes that the fast background absorbs still trigger a ``"drift"`` result.
        drift_threshold_factor: the drift score threshold is the fast per-pixel threshold
            times this factor (< 1: cumulative change is judged more sensitively).
        drift_min_area_ratio: fraction of the ROI that must differ from the slow reference.
        drift_max_area_ratio: a slow-reference difference covering more of the ROI than this
            is lighting (headlights, sunrise), not an object: the reference is refreshed silently.
        drift_min_fill: the changed pixels must fill at least this fraction of their bounding
            box; diffuse noise and wandering shadows do not.
        drift_check_seconds: minimum spacing between drift comparisons (they cost a full
            scoring pass, and slow changes need no finer sampling).
        drift_confirm_checks: consecutive drift comparisons that must agree before firing.
        drift_refresh_seconds: the slow reference is replaced after this long even if
            nothing was judged, so a stale reference cannot accumulate nuisance forever.
        recent_frames: how many recent frames the gate keeps for motion sequences (0 = none).
        recent_spacing_seconds: minimum time between two kept frames.
        recent_max_width: kept frames are subsampled to at most this width.
        recent_max_bytes: memory cap for the kept frames together.
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
    drift_enabled: bool = True
    drift_threshold_factor: float = 0.5
    drift_min_area_ratio: float = 0.004
    drift_max_area_ratio: float = 0.15
    drift_min_fill: float = 0.25
    drift_check_seconds: float = 1.0
    drift_confirm_checks: int = 2
    drift_refresh_seconds: float = 600.0
    recent_frames: int = 3
    recent_spacing_seconds: float = 0.5
    recent_max_width: int = 640
    recent_max_bytes: int = 16_000_000

    def __post_init__(self) -> None:
        """Validate every field; raise ``ValueError`` naming the first bad one."""
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
            (0.0 < self.drift_threshold_factor <= 1.0, "drift_threshold_factor must be in (0, 1]"),
            (0.0 <= self.drift_min_area_ratio < 1.0, "drift_min_area_ratio must be in [0, 1)"),
            (0.0 < self.drift_max_area_ratio <= 1.0, "drift_max_area_ratio must be in (0, 1]"),
            (0.0 <= self.drift_min_fill <= 1.0, "drift_min_fill must be in [0, 1]"),
            (self.drift_check_seconds >= 0.0, "drift_check_seconds must be >= 0"),
            (1 <= self.drift_confirm_checks <= 20, "drift_confirm_checks must be in [1, 20]"),
            (self.drift_refresh_seconds > 0.0, "drift_refresh_seconds must be > 0"),
            (0 <= self.recent_frames <= 16, "recent_frames must be in [0, 16]"),
            (self.recent_spacing_seconds >= 0.0, "recent_spacing_seconds must be >= 0"),
            (self.recent_max_width >= 16, "recent_max_width must be >= 16"),
            (self.recent_max_bytes >= 0, "recent_max_bytes must be >= 0"),
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
            raw = data.get(f.name)
            if raw is None:
                continue
            coerce = _COERCERS.get(f.type, _coerce_float)
            try:
                kwargs[f.name] = coerce(raw)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"invalid value for {f.name}: {raw!r}") from None
        return cls(**kwargs)
