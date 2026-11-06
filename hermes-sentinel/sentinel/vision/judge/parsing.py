"""Defensive parsing of model replies into verdicts."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .schema import MAX_CAPTION_LEN, MAX_ERROR_LEN, MAX_REASON_LEN
from .types import Verdict

__all__ = ["clean", "error_text", "extract_object", "parse_verdict"]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "match", "present"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "absent", "none"})


def clean(value: Any, limit: int) -> str:
    """Collapse whitespace and control characters; truncate to ``limit`` characters."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL_RE.sub(" ", value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().strip(".!").lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def parse_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        percent = value.strip().endswith("%")
        try:
            number = float(text)
        except ValueError:
            return {"high": 0.9, "medium": 0.6, "low": 0.3}.get(text.lower())
        if percent:
            number /= 100.0
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if 1.0 < number <= 100.0:  # a percentage without the sign
        number /= 100.0
    return min(1.0, number)


def extract_object(result: Any) -> dict[str, Any] | None:
    """The JSON object from a structured result (``parsed``, else the raw text)."""
    parsed = getattr(result, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    match = _JSON_OBJ_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_verdict(result: Any, *, elapsed: float = 0.0) -> Verdict:
    """Turn a host result into a :class:`Verdict` (never raises)."""
    model = clean(getattr(result, "model", ""), 120)
    obj = extract_object(result)
    if obj is None:
        return Verdict(
            None, error="model reply was not a JSON object", elapsed=elapsed, model=model
        )
    match = parse_bool(obj.get("match", obj.get("answer")))
    if match is None:
        return Verdict(
            None, error="model reply has no yes/no 'match'", elapsed=elapsed, model=model
        )
    confidence = parse_confidence(obj.get("confidence"))
    if confidence is None:
        return Verdict(
            None, error="model reply has no usable 'confidence'", elapsed=elapsed, model=model
        )
    return Verdict(
        match=match,
        confidence=confidence,
        reason=clean(obj.get("reason"), MAX_REASON_LEN),
        caption=clean(obj.get("caption"), MAX_CAPTION_LEN),
        elapsed=elapsed,
        model=model,
    )


def error_text(exc: BaseException) -> str:
    """Short human-readable description of an exception raised by the host."""
    if isinstance(exc, PermissionError):
        kind = "not permitted"
    elif isinstance(exc, TimeoutError):
        kind = "timed out"
    else:
        kind = type(exc).__name__
    detail = clean(str(exc), MAX_ERROR_LEN)
    return f"{kind}: {detail}" if detail else kind
