"""Several rules, one frame, one model call: the batch prompt, schema and reply parsing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .parsing import clean, error_text, extract_object, verdict_from_object
from .types import Verdict

__all__ = [
    "BATCH_INSTRUCTIONS",
    "BATCH_SCHEMA",
    "MAX_BATCH_RULES",
    "BatchOutcome",
    "BatchQuestion",
    "batch_questions_text",
    "batch_outcome",
    "failed_batch",
]

#: Hard ceiling on rules per call (the configured cap is clamped to this).
MAX_BATCH_RULES = 8

BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "description": "The id of the question."},
                    "match": {"type": "boolean", "description": "true = yes."},
                    "confidence": {"type": "number", "description": "Probability (0..1)."},
                    "reason": {"type": "string", "description": "Visible evidence."},
                    "caption": {"type": "string", "description": "One short sentence."},
                },
                "required": ["rule_id", "match", "confidence"],
            },
        }
    },
    "required": ["verdicts"],
}

BATCH_INSTRUCTIONS = """\
You are Sentinel, a careful visual monitor. Answer several independent yes/no questions about \
the CURRENT image. Each question has an id.

Questions:
{questions}

Rules:
- Answer every question separately, judging only what is visible in the CURRENT image. A \
question may name a region (fractions of the image width and height from the top-left); look \
only there.
- Reply with one entry per question in "verdicts", copying its id into "rule_id".
- match = true only when the answer is clearly "yes".
- confidence = how sure you are that your answer is right, from 0 to 1. Use a low value when \
the view is dark, blurred, blocked or ambiguous.
- caption = one short sentence for the user about what is happening that is relevant to the \
question. reason = the visible evidence, one sentence.
Reply with JSON only."""


@dataclass(frozen=True)
class BatchQuestion:
    """One rule's question inside a batch; ``roi`` is ``(x, y, w, h)`` fractions or None."""

    rule_id: str
    question: str
    roi: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class BatchOutcome:
    """Result of one batch call.

    ``verdicts`` holds a usable verdict per answered rule. ``missing`` lists rules the reply
    did not answer usably (the caller judges those separately); ``error`` is set when the call
    itself failed or the reply was unusable as a whole.
    """

    verdicts: dict[str, Verdict] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    error: str | None = None
    call_failed: bool = False
    elapsed: float = 0.0


def batch_questions_text(questions: Sequence[BatchQuestion]) -> str:
    """The numbered question list embedded in the prompt."""
    lines = []
    for item in questions:
        region = ""
        if item.roi is not None:
            x, y, w, h = item.roi
            region = f" [region: x={x:.2f}, y={y:.2f}, width={w:.2f}, height={h:.2f}]"
        lines.append(f'- id "{item.rule_id}": {clean(item.question, 500)}{region}')
    return "\n".join(lines)


def failed_batch(exc: BaseException, ids: Sequence[str], elapsed: float) -> BatchOutcome:
    """Outcome for a call that raised.

    The host raises ``ValueError`` when the reply does not match the schema: that is an
    unusable reply (the caller falls back to one call per rule). Anything else (timeout, no
    permission, no network) is a failed call; retrying it rule by rule would only repeat it.
    """
    return BatchOutcome(
        missing=tuple(ids),
        error=error_text(exc),
        call_failed=not isinstance(exc, ValueError),
        elapsed=elapsed,
    )


def _items(obj: dict[str, Any]) -> list[dict[str, Any]]:
    raw = obj.get("verdicts")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def batch_outcome(result: Any, ids: Sequence[str], elapsed: float) -> BatchOutcome:
    """Parse a batch reply. Unknown or duplicate ids are ignored; bad entries count as missing."""
    obj = extract_object(result)
    if obj is None:
        return BatchOutcome(
            missing=tuple(ids), error="batch reply was not a JSON object", elapsed=elapsed
        )
    model = clean(getattr(result, "model", ""), 120)
    wanted = set(ids)
    verdicts: dict[str, Verdict] = {}
    for item in _items(obj):
        rule_id = item.get("rule_id")
        if rule_id not in wanted or rule_id in verdicts:
            continue
        verdict = verdict_from_object(item, elapsed=elapsed, model=model)
        if verdict.ok:
            verdicts[rule_id] = verdict
    missing = tuple(rule_id for rule_id in ids if rule_id not in verdicts)
    error = None if verdicts else "batch reply answered none of the questions"
    return BatchOutcome(verdicts=verdicts, missing=missing, error=error, elapsed=elapsed)
