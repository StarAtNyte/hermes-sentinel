"""``sentinel_watch``: start watching a source for something described in plain English."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ....alerts import GATEWAY_INJECTION_YAML, is_chat_origin
from ....rules import (
    ACTION_AGENT,
    RECIPES,
    RuleSpec,
    build_rule,
    compile_rule,
    get_recipe,
    recipe_names,
)
from ...setup import SetupEnv, fail_titles, run_checks
from ..common import (
    ROI_PROPERTY,
    SCHEDULE_PROPERTY,
    SOURCE_PROPERTY,
    ToolContext,
    ToolError,
    ToolSpec,
    bool_arg,
    caps_text,
    num_arg,
    ok,
    parse_roi,
    parse_schedule,
    rule_brief,
    schema,
    str_arg,
)

__all__ = ["MAX_RULES", "SPEC"]

MAX_RULES = 50

SCHEMA = schema(
    "sentinel_watch",
    "Start watching a camera or the screen for something described in plain English, e.g. "
    "'tell me if someone takes my coffee mug'. Sentinel checks frames locally and only asks "
    "the vision model when something changes, then alerts in this chat (with a photo) when "
    "it happens. Use a recipe for common cases: " + ", ".join(sorted(RECIPES)) + ".",
    {
        "what": {"type": "string", "description": "What to watch for, in plain English."},
        "source": SOURCE_PROPERTY,
        "recipe": {
            "type": "string",
            "enum": sorted(RECIPES),
            "description": "Use a bundled, tuned rule instead of compiling 'what'.",
        },
        "roi": ROI_PROPERTY,
        "hold_seconds": {
            "type": "number",
            "minimum": 0,
            "description": "Require the condition to hold this long first.",
        },
        "cooldown_seconds": {
            "type": "number",
            "minimum": 0,
            "description": "Quiet time after an alert (default 300).",
        },
        "schedule": SCHEDULE_PROPERTY,
        "motion": {
            "type": "boolean",
            "description": "True when the question is about movement over time (someone "
            "walking toward the door, a level rising): the vision model then sees a short "
            "ordered sequence of recent frames. Auto-detected when omitted.",
        },
        "action_prompt": {
            "type": "string",
            "description": "Optional instruction for you (the agent) to carry "
            "out when it fires, e.g. 'turn on the porch light'.",
        },
    },
)


@dataclass(frozen=True)
class WatchRequest:
    """The parsed inputs that decide which rule gets built."""

    source: str
    recipe: str
    what: str
    roi: tuple[float, float, float, float] | None
    action_prompt: str
    origin: dict[str, Any]


def _rule_from_recipe(context: ToolContext, request: WatchRequest) -> RuleSpec:
    recipe = get_recipe(request.recipe)
    if recipe is None:
        raise ToolError(
            f"unknown recipe {request.recipe!r}; choose one of " + ", ".join(recipe_names())
        )
    return build_rule(
        recipe,
        request.source,
        text=request.what,
        defaults=context.sentinel.config.rule_defaults(),
        origin=request.origin,
        roi=request.roi,
        action_prompt=request.action_prompt,
    )


def _rule_from_text(context: ToolContext, request: WatchRequest) -> RuleSpec:
    if not request.what:
        raise ToolError("say what to watch for in 'what' (or pick a 'recipe')")
    if not context.compile_cap.take():
        raise ToolError(
            f"rule compiling is limited to {context.compile_cap.cap} per hour; use a "
            "recipe or try again later"
        )
    sentinel = context.sentinel
    spec = compile_rule(
        request.what,
        request.source,
        getattr(sentinel.ctx, "llm", None),
        defaults=sentinel.config.rule_defaults(),
        origin=request.origin,
    )
    changes: dict[str, Any] = {}
    if request.roi is not None:
        changes["roi"] = request.roi
    if request.action_prompt:
        changes.update(action=ACTION_AGENT, action_prompt=request.action_prompt)
    return spec.with_changes(**changes) if changes else spec


def _timing_changes(args: Mapping[str, Any], spec: RuleSpec) -> dict[str, Any]:
    """Overrides the caller gave for hold, cooldown, schedule and motion."""
    changes: dict[str, Any] = {}
    hold = num_arg(args, "hold_seconds")
    if hold is not None:
        changes["hold_seconds"] = hold
        if hold > 0 and spec.mode == "trigger":
            changes["mode"] = "condition"
    cooldown = num_arg(args, "cooldown_seconds")
    if cooldown is not None:
        changes["cooldown_seconds"] = cooldown
    if args.get("schedule") is not None:
        changes["schedule"] = parse_schedule(args.get("schedule"))
    if args.get("motion") is not None:
        changes["motion"] = bool_arg(args, "motion")
    return changes


def _here_hint(context: ToolContext, origin: Mapping[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Ask for ``/sentinel here`` once per session while the routing key is unknown."""
    if origin.get("session_key") or origin.get("chat_id"):
        return ""
    who = str(origin.get("session_id") or kwargs.get("session_id") or "?")
    if not context.here_hints.first_time(who):
        return ""
    where = "this chat" if is_chat_origin(origin) else "this session"
    return f" To make sure alerts come back to {where}, ask the user to send '/sentinel here' once."


def _setup_hint(context: ToolContext) -> str:
    """Point at ``/sentinel setup`` while a fail-level check exists (cheap checks only)."""
    try:
        env = SetupEnv.from_sentinel(context.sentinel, probe_sources=False)
        failing = fail_titles(run_checks(env))
    except Exception:  # noqa: BLE001 - a hint must never break a watch
        return ""
    if not failing:
        return ""
    return (
        f" Setup problem ({', '.join(failing)}): run /sentinel setup "
        "(or the sentinel_setup_check tool) for the fix."
    )


def _confirmation(
    context: ToolContext, saved: RuleSpec, origin: Mapping[str, Any], kwargs: Mapping[str, Any]
) -> str:
    """The reply text: what is watched, the cap, the delivery opt-in and any hints."""
    hold_text = (
        f" once it has held for {saved.hold_seconds:g}s" if saved.effective_hold_seconds else ""
    )
    message = (
        f'Watching "{saved.source}" for: "{saved.question}"{hold_text}. Checks only when '
        f"something moves. Cap: {caps_text(context.sentinel.config)}."
    )
    if context.sentinel.engine.standing_by:
        message += " (Another Hermes process is running the watcher; it will pick this up.)"
    message += (
        " Alerts are injected into the chat, which Hermes only allows outside the classic "
        "CLI when the user has set this in config.yaml (it is off by default):\n"
        + GATEWAY_INJECTION_YAML
    )
    return message + _here_hint(context, origin, kwargs) + _setup_hint(context)


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Build, save and start one rule from a recipe or a plain-English description."""
    source = context.source_arg(args)
    recipe = str_arg(args, "recipe", limit=64)
    what = str_arg(args, "what")
    roi = parse_roi(args.get("roi"))
    action_prompt = str_arg(args, "action_prompt")
    if len(context.sentinel.store.list_rules()) >= MAX_RULES:
        raise ToolError(f"too many rules ({MAX_RULES}); delete some with sentinel_update")
    origin = context.caller(kwargs)
    request = WatchRequest(source, recipe, what, roi, action_prompt, origin)
    spec = _rule_from_recipe(context, request) if recipe else _rule_from_text(context, request)
    changes = _timing_changes(args, spec)
    if changes:
        spec = spec.with_changes(**changes)
    saved = context.save_and_apply(spec)
    return ok(rule=rule_brief(saved), message=_confirmation(context, saved, origin, kwargs))


SPEC = ToolSpec("sentinel_watch", SCHEMA, "\U0001f441", run)
