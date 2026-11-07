"""Plain-English watch rules: the ``RuleSpec`` model, its state machine and the rule compiler.

``RuleSpec``
    The persisted description of one watch rule (question, mode, timing, ROI, schedule,
    action). Validated by ``from_dict`` and ``validate()``; round-trips through JSON.

``RuleStateMachine``
    Turns a stream of judge verdicts into fire decisions. Pure (no I/O) and clock-injected.
    States: ``idle -> candidate -> firing -> cooldown``.

``compile_rule(text, source, llm)``
    One structured LLM call that turns plain English into a ``RuleSpec``, with a deterministic
    fallback when no LLM is available or the call fails.

Rule modes: ``trigger`` fires once when the question becomes true; ``condition`` fires when it
has been true for ``hold_seconds``; ``absence`` treats a confident *negative* as evidence and
fires when an expected thing has been missing for ``hold_seconds``.

Rule compilation is text-only, so it uses its own auxiliary task ``sentinel_rules``
(``RULES_TASK``) rather than the vision task. If the task is not registered, compilation
falls back to the deterministic parser instead of failing.
"""

from __future__ import annotations

from .calibration import lowered_threshold, raised_threshold
from .compiler import COMPILE_INSTRUCTIONS, COMPILE_SCHEMA, compile_rule, fallback_rule
from .compiler.motion import looks_like_motion
from .constants import (
    ACTION_AGENT,
    ACTION_NOTIFY,
    ACTIONS,
    COMPILED_BY_FALLBACK,
    COMPILED_BY_LLM,
    COMPILED_BY_MANUAL,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_CONFIRM_K,
    DEFAULT_CONFIRM_N,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    MAX_ACTION_PROMPT_LEN,
    MAX_CONFIRM_N,
    MAX_COOLDOWN_SECONDS,
    MAX_HOLD_SECONDS,
    MAX_ORIGIN_KEYS,
    MAX_ORIGIN_VALUE_LEN,
    MAX_QUESTION_LEN,
    MAX_TEXT_LEN,
    MODE_ABSENCE,
    MODE_CONDITION,
    MODE_TRIGGER,
    MODES,
    RULES_TASK,
    RULES_TASK_INFO,
)
from .errors import RuleValidationError
from .machine import FireEvent, RuleState, RuleStateMachine, StepResult
from .recipes import RECIPES, Recipe, build_rule, get_recipe, recipe_names
from .schedule import Schedule
from .spec import RuleSpec
from .validation import new_rule_id

__all__ = [
    "ACTIONS",
    "ACTION_AGENT",
    "ACTION_NOTIFY",
    "COMPILED_BY_FALLBACK",
    "COMPILED_BY_LLM",
    "COMPILED_BY_MANUAL",
    "COMPILE_INSTRUCTIONS",
    "COMPILE_SCHEMA",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_CONFIRM_K",
    "DEFAULT_CONFIRM_N",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_STALE_AFTER_SECONDS",
    "MAX_ACTION_PROMPT_LEN",
    "MAX_CONFIRM_N",
    "MAX_COOLDOWN_SECONDS",
    "MAX_HOLD_SECONDS",
    "MAX_ORIGIN_KEYS",
    "MAX_ORIGIN_VALUE_LEN",
    "MAX_QUESTION_LEN",
    "MAX_TEXT_LEN",
    "MODES",
    "MODE_ABSENCE",
    "MODE_CONDITION",
    "MODE_TRIGGER",
    "RECIPES",
    "RULES_TASK",
    "RULES_TASK_INFO",
    "FireEvent",
    "Recipe",
    "RuleSpec",
    "RuleState",
    "RuleStateMachine",
    "RuleValidationError",
    "Schedule",
    "StepResult",
    "build_rule",
    "compile_rule",
    "fallback_rule",
    "get_recipe",
    "looks_like_motion",
    "lowered_threshold",
    "new_rule_id",
    "raised_threshold",
    "recipe_names",
]
