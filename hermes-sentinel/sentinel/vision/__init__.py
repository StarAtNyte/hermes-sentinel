"""Vision pipeline: the cheap local change gate and the model-backed judge.

The gate lives in :mod:`.gate` and imports numpy, so it is not imported here; import it
only once frames flow (``from .vision.gate import ChangeGate``).
"""

from .imaging import crop_roi, encode_image, encode_jpeg
from .judge import (
    DESCRIBE_SCHEMA,
    VERDICT_SCHEMA,
    VISION_TASK,
    VISION_TASK_INFO,
    Description,
    Example,
    Judge,
    Verdict,
    parse_verdict,
)

__all__ = [
    "DESCRIBE_SCHEMA",
    "VERDICT_SCHEMA",
    "VISION_TASK",
    "VISION_TASK_INFO",
    "Description",
    "Example",
    "Judge",
    "Verdict",
    "crop_roi",
    "encode_image",
    "encode_jpeg",
    "parse_verdict",
]
