"""Plain English to :class:`RuleSpec` via one structured LLM call, with an offline fallback."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from ..constants import (
    ACTION_AGENT,
    COMPILED_BY_LLM,
    MAX_ACTION_PROMPT_LEN,
    MAX_COOLDOWN_SECONDS,
    MAX_HOLD_SECONDS,
    MAX_QUESTION_LEN,
    MODE_CONDITION,
    MODE_TRIGGER,
    MODES,
    RULES_TASK,
)
from ..errors import RuleValidationError
from ..schedule import Schedule
from ..spec import RuleSpec
from ..validation import validate_roi
from .fallback import clean_text, fallback_rule, rule_defaults
from .schema import COMPILE_INSTRUCTIONS, COMPILE_SCHEMA

logger = logging.getLogger(__name__)


def _bounded_number(data: Mapping[str, Any], key: str, upper: float) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, upper)


def _coerce_question(data: Mapping[str, Any]) -> str:
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise RuleValidationError("model returned no question")
    question = " ".join(question.split())
    if len(question) > MAX_QUESTION_LEN:
        question = question[: MAX_QUESTION_LEN - 1].rstrip() + "?"
    return question


def _coerce_timing(data: Mapping[str, Any], out: dict[str, Any]) -> None:
    mode = data.get("mode")
    out["mode"] = mode if mode in MODES else MODE_TRIGGER
    hold = _bounded_number(data, "hold_seconds", MAX_HOLD_SECONDS)
    if hold is not None:
        out["hold_seconds"] = hold
    cooldown = _bounded_number(data, "cooldown_seconds", MAX_COOLDOWN_SECONDS)
    if cooldown is not None:
        out["cooldown_seconds"] = cooldown
    if out["mode"] == MODE_TRIGGER and out.get("hold_seconds", 0) > 0:
        # A trigger with a duration is really a condition.
        out["mode"] = MODE_CONDITION


def _coerce_region_and_schedule(data: Mapping[str, Any], out: dict[str, Any]) -> None:
    try:
        out["roi"] = validate_roi(data.get("roi"))
    except RuleValidationError:
        logger.info("sentinel: dropping invalid ROI from compiled rule: %r", data.get("roi"))
    if data.get("schedule") is None:
        return
    try:
        out["schedule"] = Schedule.from_dict(data["schedule"])
    except RuleValidationError:
        logger.info("sentinel: dropping invalid schedule: %r", data.get("schedule"))


def coerce_llm_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Salvage what we can from the model's output; invalid optional fields are dropped."""
    out: dict[str, Any] = {"question": _coerce_question(data)}
    _coerce_timing(data, out)
    _coerce_region_and_schedule(data, out)
    prompt = data.get("action_prompt")
    prompt = " ".join(prompt.split())[:MAX_ACTION_PROMPT_LEN] if isinstance(prompt, str) else ""
    if data.get("action") == ACTION_AGENT and prompt:
        out["action"] = ACTION_AGENT
        out["action_prompt"] = prompt
    return out


def _call_llm(
    llm: Any, cleaned: str, source: str, *, task: str | None, timeout: float | None
) -> Mapping[str, Any]:
    """One structured completion; returns the parsed JSON object."""
    result = llm.complete_structured(
        instructions=COMPILE_INSTRUCTIONS,
        input=[{"type": "text", "text": f"Request: {cleaned}\nSource: {source.strip()}"}],
        json_schema=COMPILE_SCHEMA,
        schema_name="sentinel_rule",
        temperature=0.0,
        max_tokens=400,
        timeout=timeout,
        purpose="sentinel:compile",
        task=task,
    )
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, Mapping):
        raise RuleValidationError("model returned no JSON object")
    return parsed


def compile_rule(
    text: str,
    source: str,
    llm: Any | None,
    *,
    defaults: Mapping[str, Any] | None = None,
    origin: Mapping[str, Any] | None = None,
    task: str | None = RULES_TASK,
    timeout: float | None = 30.0,
) -> RuleSpec:
    """Turn a plain-English request into a validated :class:`RuleSpec`.

    Makes one ``llm.complete_structured`` call (``task=RULES_TASK``,
    ``purpose="sentinel:compile"``). Any failure - no LLM, trust refusal, timeout, schema
    mismatch, unusable output - falls back to :func:`fallback_rule`; this function only
    raises ``ValueError`` for empty/non-string ``text`` or ``source``. Check
    ``spec.compiled_by`` (``"llm"`` / ``"fallback"``) to see which path was taken.

    ``defaults`` may carry ``confirm_k``, ``confirm_n``, ``confidence_threshold`` and
    ``cooldown_seconds`` from plugin config; an explicit cooldown in the request wins.
    """
    cleaned = clean_text(text)
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if llm is None:
        return fallback_rule(cleaned, source, defaults=defaults, origin=origin)
    try:
        parsed = _call_llm(llm, cleaned, source, task=task, timeout=timeout)
        base = {**rule_defaults(defaults), **coerce_llm_fields(parsed)}
        return RuleSpec(
            source=source,
            text=cleaned,
            compiled_by=COMPILED_BY_LLM,
            origin=dict(origin or {}),
            **base,
        ).validate()
    except Exception as exc:  # noqa: BLE001 - any LLM failure degrades to the fallback
        logger.warning("sentinel: rule compiler fell back to the offline parser: %s", exc)
        return fallback_rule(cleaned, source, defaults=defaults, origin=origin)
