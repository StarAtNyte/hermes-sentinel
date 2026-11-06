"""Group-by-group validation of a :class:`RuleSpec`, run in a fixed order by ``validate()``.

Each function normalises the spec in place and raises :class:`RuleValidationError`. The
order of the checks (and so which error a doubly-broken spec reports) is part of behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    ACTION_AGENT,
    ACTIONS,
    COMPILED_BY_VALUES,
    MAX_ACTION_PROMPT_LEN,
    MAX_CONFIRM_N,
    MAX_COOLDOWN_SECONDS,
    MAX_HOLD_SECONDS,
    MAX_QUESTION_LEN,
    MAX_TEXT_LEN,
    MODES,
    RULE_ID_RE,
)
from .errors import RuleValidationError
from .schedule import Schedule
from .validation import finite, to_int, validate_origin, validate_roi

if TYPE_CHECKING:
    from .spec import RuleSpec


def validate_text_fields(spec: RuleSpec) -> None:
    """Check ``id``, ``question``, ``source`` and ``text``."""
    if not isinstance(spec.id, str) or not RULE_ID_RE.match(spec.id):
        raise RuleValidationError(f"id must match {RULE_ID_RE.pattern}, got {spec.id!r}")
    if not isinstance(spec.question, str) or not spec.question.strip():
        raise RuleValidationError("question must be a non-empty string")
    spec.question = spec.question.strip()
    if len(spec.question) > MAX_QUESTION_LEN:
        raise RuleValidationError(f"question longer than {MAX_QUESTION_LEN} characters")
    if not isinstance(spec.source, str) or not spec.source.strip():
        raise RuleValidationError("source must be a non-empty string")
    spec.source = spec.source.strip()
    if not isinstance(spec.text, str):
        raise RuleValidationError("text must be a string")
    if len(spec.text) > MAX_TEXT_LEN:
        raise RuleValidationError(f"text longer than {MAX_TEXT_LEN} characters")


def validate_timing(spec: RuleSpec) -> None:
    """Check ``mode``, ``hold_seconds``, ``cooldown_seconds``, ``roi`` and ``schedule``."""
    if spec.mode not in MODES:
        raise RuleValidationError(f"mode must be one of {MODES}, got {spec.mode!r}")
    spec.hold_seconds = finite(spec.hold_seconds, "hold_seconds")
    if not 0 <= spec.hold_seconds <= MAX_HOLD_SECONDS:
        raise RuleValidationError(f"hold_seconds must be 0..{MAX_HOLD_SECONDS:g}")
    spec.cooldown_seconds = finite(spec.cooldown_seconds, "cooldown_seconds")
    if not 0 <= spec.cooldown_seconds <= MAX_COOLDOWN_SECONDS:
        raise RuleValidationError(f"cooldown_seconds must be 0..{MAX_COOLDOWN_SECONDS:g}")
    spec.roi = validate_roi(spec.roi)
    if spec.schedule is not None and not isinstance(spec.schedule, Schedule):
        spec.schedule = Schedule.from_dict(spec.schedule)


def validate_action(spec: RuleSpec) -> None:
    """Check ``action`` and ``action_prompt``."""
    if spec.action not in ACTIONS:
        raise RuleValidationError(f"action must be one of {ACTIONS}, got {spec.action!r}")
    if not isinstance(spec.action_prompt, str):
        raise RuleValidationError("action_prompt must be a string")
    if len(spec.action_prompt) > MAX_ACTION_PROMPT_LEN:
        raise RuleValidationError("action_prompt is too long")
    if spec.action == ACTION_AGENT and not spec.action_prompt.strip():
        raise RuleValidationError("action 'agent' needs a non-empty action_prompt")


def validate_confirmation(spec: RuleSpec) -> None:
    """Check the k-of-n window and the confidence threshold."""
    spec.confirm_k = to_int(spec.confirm_k, "confirm_k")
    spec.confirm_n = to_int(spec.confirm_n, "confirm_n")
    if not 1 <= spec.confirm_n <= MAX_CONFIRM_N:
        raise RuleValidationError(f"confirm_n must be 1..{MAX_CONFIRM_N}")
    if not 1 <= spec.confirm_k <= spec.confirm_n:
        raise RuleValidationError("confirm_k must be 1..confirm_n")
    spec.confidence_threshold = finite(spec.confidence_threshold, "confidence_threshold")
    if not 0 <= spec.confidence_threshold <= 1:
        raise RuleValidationError("confidence_threshold must be 0..1")


def validate_metadata(spec: RuleSpec) -> None:
    """Check ``enabled``, ``recipe``, ``compiled_by``, ``origin`` and the timestamps."""
    if not isinstance(spec.enabled, bool):
        raise RuleValidationError("enabled must be a boolean")
    if spec.recipe is not None and (not isinstance(spec.recipe, str) or not spec.recipe):
        raise RuleValidationError("recipe must be a non-empty string or null")
    if spec.compiled_by not in COMPILED_BY_VALUES:
        raise RuleValidationError(f"compiled_by must be one of {COMPILED_BY_VALUES}")
    spec.origin = validate_origin(spec.origin)
    for name in ("created_at", "updated_at"):
        value = getattr(spec, name)
        if not isinstance(value, str) or not value:
            raise RuleValidationError(f"{name} must be an ISO-8601 string")
