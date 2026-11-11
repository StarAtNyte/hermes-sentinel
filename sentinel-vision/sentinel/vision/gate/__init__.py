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

The structural difference fits ``current ~= a * reference + b`` over a small window and
keeps the variance the fit cannot explain, so smooth lighting changes are absorbed while new
structure is not. A pixel only counts as changed when it is new relative to *both*
references. If more than ``global_change_max`` of the frame changes at once, the gate
re-baselines. A periodic ``tick`` every ``idle_check_seconds`` lets state rules be re-judged
without motion.

numpy is the only hard dependency. When OpenCV is importable the heavy filters run through
it. Importing this package imports numpy, so only import it once frames flow.
"""

from .change_gate import ChangeGate
from .config import GateConfig
from .geometry import BBox, Roi, bbox_iou, normalize_roi, roi_from_pixels
from .ops import cv2_available
from .types import GateResult

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
