"""Vision-judge constants, JSON schemas and prompt templates."""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_TIMEOUT",
    "DESCRIBE_INSTRUCTIONS",
    "DESCRIBE_SCHEMA",
    "JUDGE_INSTRUCTIONS",
    "MAX_ANSWER_LEN",
    "MAX_CAPTION_LEN",
    "MAX_ERROR_LEN",
    "MAX_EXAMPLES_PER_KIND",
    "MAX_EXAMPLE_BYTES",
    "MAX_REASON_LEN",
    "VERDICT_SCHEMA",
    "VISION_TASK",
    "VISION_TASK_INFO",
]

VISION_TASK = "sentinel_vision"
#: Keyword arguments for ``ctx.register_auxiliary_task(VISION_TASK, **VISION_TASK_INFO)``.
VISION_TASK_INFO: dict[str, Any] = {
    "display_name": "Sentinel vision judge",
    "description": "Looks at camera or screen frames and answers a watch rule's yes/no "
    "question (needs a vision-capable model).",
}

DEFAULT_TIMEOUT = 60.0
MAX_EXAMPLES_PER_KIND = 2
MAX_EXAMPLE_BYTES = 4 * 1024 * 1024
MAX_CAPTION_LEN = 300
MAX_REASON_LEN = 600
MAX_ANSWER_LEN = 1500
MAX_ERROR_LEN = 300

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean", "description": "Answer to the question: true = yes."},
        "confidence": {
            "type": "number",
            "description": "Probability (0..1) that the answer is right.",
        },
        "reason": {"type": "string", "description": "Visible evidence, one sentence."},
        "caption": {
            "type": "string",
            "description": "One short sentence describing what is happening, for the user.",
        },
    },
    "required": ["match", "confidence"],
}

DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Answer to the question, or a description."},
        "caption": {"type": "string", "description": "One short sentence summary."},
    },
    "required": ["answer"],
}

JUDGE_INSTRUCTIONS = """\
You are Sentinel, a careful visual monitor. Answer one yes/no question about the CURRENT image.

Question: {question}

Rules:
- Judge only what is visible in the CURRENT image (and its close-up, if given). Labelled \
reference images are examples from the past, not the current scene.
- match = true only when the answer to the question is clearly "yes".
- confidence = how sure you are that your answer is right, from 0 to 1. Use a low value when \
the view is dark, blurred, blocked or ambiguous.
- caption = one short sentence for the user describing what is happening that is relevant \
to the question (for example "a person in a grey hoodie picked up the mug").
- reason = the visible evidence, one sentence.
Reply with JSON only."""

DESCRIBE_INSTRUCTIONS = """\
You are Sentinel, looking at a live camera or screen image for the user.
{task}
Be concrete and brief. Do not guess about things you cannot see.
Reply with JSON only: "answer" (the answer or description) and "caption" (one short sentence)."""
