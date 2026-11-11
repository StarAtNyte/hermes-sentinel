"""Second opinion: a differently worded look at a higher-resolution crop, and how to combine."""

from __future__ import annotations

from dataclasses import replace

from .parsing import clean
from .schema import MAX_REASON_LEN
from .types import Verdict

__all__ = [
    "VERIFY_INSTRUCTIONS",
    "VERIFY_JPEG_QUALITY",
    "VERIFY_MAX_SIDE",
    "combine_verdicts",
]

#: Second opinions look at a crop at higher resolution than the normal 1024 px frame.
VERIFY_MAX_SIDE = 1536
VERIFY_JPEG_QUALITY = 90

VERIFY_INSTRUCTIONS = """\
You are a strict, independent reviewer of a security-style camera alert. Someone else may \
have answered a yes/no question about this scene, and your job is to check it from the \
evidence alone.

Question: {question}

You are given a close-up, at higher resolution, of the part of the scene that just changed.

Rules:
- Say "yes" only if you can point to specific visible evidence in the close-up. If the \
evidence is unclear, partial, cut off or could be something else, say "no" with low \
confidence.
- match = true means the answer to the question is yes.
- confidence = how sure you are that your answer is right, from 0 to 1.
- reason = the specific visible evidence, one sentence.
- caption = one short sentence for the user describing what you see.
Reply with JSON only."""


def combine_verdicts(first: Verdict, second: Verdict) -> Verdict:
    """Merge the first verdict with its second opinion.

    * Second opinion unusable: the first verdict stands unchanged (a failed check must neither
      confirm nor cancel an alert).
    * Opinions agree: the confidence is the average of the two.
    * Opinions disagree: the second (closer, higher-resolution) look wins, with a reason that
      says so. For a trigger/condition rule that turns a hit into a miss, and for an absence
      rule too, because ``match`` flips away from the evidence side.
    """
    if not second.ok:
        return first
    elapsed = first.elapsed + second.elapsed
    if first.match != second.match:
        note = clean(f"Second opinion disagreed: {second.reason}", MAX_REASON_LEN)
        return replace(
            second,
            reason=note,
            caption=second.caption or first.caption,
            elapsed=elapsed,
            model=second.model or first.model,
        )
    return replace(
        first,
        confidence=(first.confidence + second.confidence) / 2.0,
        reason=first.reason or second.reason,
        caption=first.caption or second.caption,
        elapsed=elapsed,
    )
