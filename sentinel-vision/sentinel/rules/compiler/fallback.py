"""Deterministic, offline rule parsing used when no LLM is available."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from ..constants import (
    COMPILED_BY_FALLBACK,
    MAX_HOLD_SECONDS,
    MAX_QUESTION_LEN,
    MAX_TEXT_LEN,
    MODE_CONDITION,
    MODE_TRIGGER,
)
from ..errors import RuleValidationError
from ..spec import RuleSpec
from .motion import looks_like_motion

logger = logging.getLogger(__name__)

_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:hey|ok|okay)\s+\w+[,\s]+)?"
    r"(?:(?:can|could|would)\s+you\s+)?"
    r"(?:(?:tell|alert|notify|ping|text|message|warn|wake)\s+me\s+(?:when(?:ever)?|if|once)"
    r"|let\s+me\s+know\s+(?:when(?:ever)?|if|once)"
    r"|watch\s+(?:for|if|whether|when)"
    r"|look\s+(?:out\s+)?for"
    r"|check\s+(?:if|whether)"
    r"|(?:when(?:ever)?|if))\s+",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\b(?:for|over|longer\s+than|more\s+than|at\s+least)\s+"
    r"(\d+(?:\.\d+)?|an?|one)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}
_DEFAULT_KEYS = ("confirm_k", "confirm_n", "confidence_threshold", "cooldown_seconds")


def duration_seconds(amount: str, unit: str) -> float:
    """Seconds in a phrase such as ``("5", "minutes")`` or ``("an", "hour")``."""
    amount = amount.lower()
    value = 1.0 if amount in ("a", "an", "one") else float(amount)
    return value * _UNIT_SECONDS[unit.lower()[0]]


def clean_text(text: Any) -> str:
    """Whitespace-normalised request text; raises ``ValueError`` if empty or not a string."""
    if not isinstance(text, str):
        raise ValueError("rule text must be a string")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("rule text is empty")
    return cleaned[:MAX_TEXT_LEN]


def rule_defaults(defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pick the config-driven defaults that apply to a new rule; bad values are dropped."""
    out: dict[str, Any] = {}
    if not defaults:
        return out
    for key in _DEFAULT_KEYS:
        if key in defaults and defaults[key] is not None:
            out[key] = defaults[key]
    try:
        probe = RuleSpec(question="probe", source="probe", **out)
        probe.validate()
    except (RuleValidationError, TypeError):
        logger.warning("sentinel: ignoring invalid rule defaults %r", out)
        return {}
    return {k: getattr(probe, k) for k in out}


def _split_duration(body: str) -> tuple[str, str, float]:
    """``(body, mode, hold)`` after pulling a "for 5 minutes" phrase out of ``body``."""
    match = _DURATION_RE.search(body)
    if not match:
        return body, MODE_TRIGGER, 0.0
    hold = min(MAX_HOLD_SECONDS, duration_seconds(match.group(1), match.group(2)))
    if hold <= 0:
        return body, MODE_TRIGGER, hold
    stripped = (body[: match.start()] + body[match.end() :]).strip(" ,.;")
    return " ".join(stripped.split()) or body, MODE_CONDITION, hold


def fallback_rule(
    text: str,
    source: str,
    *,
    defaults: Mapping[str, Any] | None = None,
    origin: Mapping[str, Any] | None = None,
) -> RuleSpec:
    """Deterministic, offline rule from plain text.

    The question is the request with the leading "tell me if/when ..." stripped. A duration
    phrase ("for 5 minutes") turns the rule into a ``condition`` with that hold.
    """
    cleaned = clean_text(text)
    body = _PREFIX_RE.sub("", cleaned, count=1).strip() or cleaned
    body, mode, hold = _split_duration(body)
    body = body.rstrip(" ?.!")
    question = f"Does the image show this: {body}?"
    if len(question) > MAX_QUESTION_LEN:
        question = question[: MAX_QUESTION_LEN - 1].rstrip() + "?"
    spec = RuleSpec(
        question=question,
        source=source,
        text=cleaned,
        mode=mode,
        hold_seconds=hold,
        motion=looks_like_motion(body),
        compiled_by=COMPILED_BY_FALLBACK,
        origin=dict(origin or {}),
        **rule_defaults(defaults),
    )
    return spec.validate()
