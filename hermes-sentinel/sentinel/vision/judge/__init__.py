"""Vision judge: asks the host's vision model a yes/no question about a frame.

One ``ctx.llm.complete_structured`` call per judgement, on the plugin's own auxiliary task
``sentinel_vision`` (``VISION_TASK``) so users can route it to any vision-capable model:

* the current frame, JPEG-encoded with its longest side at most ``max_side`` (1024) pixels;
* a close-up crop of the rule's ROI when the ROI is a small part of the frame;
* up to two positive and two negative teaching examples, each preceded by a text block
  saying which answer it shows.

The reply must match :data:`VERDICT_SCHEMA`. Parsing is defensive and every failure becomes
``Verdict(match=None, error=...)``. Nothing here raises to the caller.
"""

from .judge import Judge
from .parsing import parse_verdict
from .schema import (
    DESCRIBE_SCHEMA,
    MAX_CAPTION_LEN,
    MAX_EXAMPLE_BYTES,
    VERDICT_SCHEMA,
    VISION_TASK,
    VISION_TASK_INFO,
)
from .types import Description, Example, Verdict

__all__ = [
    "DESCRIBE_SCHEMA",
    "MAX_CAPTION_LEN",
    "MAX_EXAMPLE_BYTES",
    "VERDICT_SCHEMA",
    "VISION_TASK",
    "VISION_TASK_INFO",
    "Description",
    "Example",
    "Judge",
    "Verdict",
    "parse_verdict",
]
