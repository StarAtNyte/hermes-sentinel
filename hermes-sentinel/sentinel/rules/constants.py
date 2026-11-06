"""Rule modes, actions, limits, defaults and the auxiliary LLM task used by the compiler."""

from __future__ import annotations

import re
from typing import Any

MODE_TRIGGER = "trigger"
MODE_CONDITION = "condition"
MODE_ABSENCE = "absence"
MODES: tuple[str, ...] = (MODE_TRIGGER, MODE_CONDITION, MODE_ABSENCE)

ACTION_NOTIFY = "notify"
ACTION_AGENT = "agent"
ACTIONS: tuple[str, ...] = (ACTION_NOTIFY, ACTION_AGENT)

COMPILED_BY_LLM = "llm"
COMPILED_BY_FALLBACK = "fallback"
COMPILED_BY_MANUAL = "manual"
COMPILED_BY_VALUES = (COMPILED_BY_LLM, COMPILED_BY_FALLBACK, COMPILED_BY_MANUAL)

#: Auxiliary LLM task used by ``compile_rule`` (text-only).
RULES_TASK = "sentinel_rules"
#: Keyword arguments for ``ctx.register_auxiliary_task(RULES_TASK, **RULES_TASK_INFO)``.
RULES_TASK_INFO: dict[str, Any] = {
    "display_name": "Sentinel rule compiler",
    "description": "Turns a plain-English watch request into a structured Sentinel rule "
    "(text only; a small fast model is enough).",
}

MAX_TEXT_LEN = 2000
MAX_QUESTION_LEN = 500
MAX_ACTION_PROMPT_LEN = 2000
MAX_HOLD_SECONDS = 7 * 24 * 3600.0
MAX_COOLDOWN_SECONDS = 7 * 24 * 3600.0
MAX_CONFIRM_N = 20
MAX_ORIGIN_KEYS = 32
MAX_ORIGIN_VALUE_LEN = 512

RULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_CONFIRM_K = 2
DEFAULT_CONFIRM_N = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_STALE_AFTER_SECONDS = 900.0
