"""``sentinel_update``: pause, resume, delete or edit a watch rule."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ....rules import ACTION_AGENT, ACTION_NOTIFY, MODES
from ..common import (
    ROI_PROPERTY,
    SCHEDULE_PROPERTY,
    ToolContext,
    ToolError,
    ToolSpec,
    bool_arg,
    num_arg,
    ok,
    parse_roi,
    parse_schedule,
    rule_brief,
    schema,
    str_arg,
)

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_update",
    "Pause, resume, delete or edit a watch rule.",
    {
        "rule_id": {"type": "string", "description": "The rule id (see sentinel_rules)."},
        "action": {"type": "string", "enum": ["pause", "resume", "delete", "edit"]},
        "changes": {
            "type": "object",
            "description": "For action=edit: fields to change.",
            "properties": {
                "question": {"type": "string", "description": "New yes/no visual question."},
                "mode": {"type": "string", "enum": list(MODES)},
                "hold_seconds": {"type": "number", "minimum": 0},
                "cooldown_seconds": {"type": "number", "minimum": 0},
                "confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                "roi": {**ROI_PROPERTY, "type": ["array", "null"]},
                "source": {"type": "string"},
                "action_prompt": {
                    "type": "string",
                    "description": "Empty string = just notify.",
                },
                "schedule": {**SCHEDULE_PROPERTY, "type": ["object", "null"]},
                "motion": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "purge_events": {
            "type": "boolean",
            "description": "With delete: also delete its events and photos.",
        },
    },
    ["rule_id", "action"],
)


def _number_change(key: str) -> Callable[[ToolContext, Mapping[str, Any]], dict[str, Any]]:
    return lambda _context, raw: {key: num_arg(raw, key)}


def _question_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"question": str_arg(raw, "question", required=True, limit=500)}


def _mode_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    mode = str_arg(raw, "mode", required=True, limit=16)
    if mode not in MODES:
        raise ToolError(f"mode must be one of {list(MODES)}")
    return {"mode": mode}


def _roi_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"roi": parse_roi(raw.get("roi"))}


def _source_change(context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"source": context.source_arg(raw)}


def _action_prompt_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    prompt = str_arg(raw, "action_prompt")
    return {"action_prompt": prompt, "action": ACTION_AGENT if prompt else ACTION_NOTIFY}


def _schedule_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"schedule": parse_schedule(raw.get("schedule"))}


def _motion_change(_context: ToolContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"motion": bool_arg(raw, "motion")}


#: Editable fields in the order they are validated; each parser returns the rule changes.
_EDIT_FIELDS: dict[str, Callable[[ToolContext, Mapping[str, Any]], dict[str, Any]]] = {
    "hold_seconds": _number_change("hold_seconds"),
    "cooldown_seconds": _number_change("cooldown_seconds"),
    "confidence_threshold": _number_change("confidence_threshold"),
    "question": _question_change,
    "mode": _mode_change,
    "roi": _roi_change,
    "source": _source_change,
    "action_prompt": _action_prompt_change,
    "schedule": _schedule_change,
    "motion": _motion_change,
}


def _edit_changes(context: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    raw = args.get("changes")
    if not isinstance(raw, Mapping) or not raw:
        raise ToolError("action=edit needs a non-empty 'changes' object")
    unknown = sorted(set(raw) - set(_EDIT_FIELDS))
    if unknown:
        raise ToolError(f"cannot change {unknown}; allowed: {sorted(_EDIT_FIELDS)}")
    changes: dict[str, Any] = {}
    for key, parse in _EDIT_FIELDS.items():
        if key in raw:
            changes.update(parse(context, raw))
    return changes


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Apply ``action`` (pause, resume, delete or edit) to one rule the caller can see."""
    rule_id = str_arg(args, "rule_id", required=True, limit=64)
    action = str_arg(args, "action", required=True, limit=16).lower()
    spec = context.rule(rule_id, context.caller(kwargs))
    if action in ("pause", "resume"):
        saved = context.save_and_apply(spec.with_changes(enabled=action == "resume"))
        verb = "Paused" if action == "pause" else "Resumed"
        return ok(rule=rule_brief(saved), message=f"{verb} rule {rule_id}.")
    if action == "delete":
        return _delete(context, rule_id, bool(args.get("purge_events", False)))
    if action != "edit":
        raise ToolError("action must be pause, resume, delete or edit")
    saved = context.save_and_apply(spec.with_changes(**_edit_changes(context, args)))
    return ok(rule=rule_brief(saved), message=f"Updated rule {rule_id}.")


def _delete(context: ToolContext, rule_id: str, purge: bool) -> str:
    context.sentinel.engine.remove_rule(rule_id)
    context.sentinel.store.delete_rule(rule_id, purge_events=purge)
    return ok(
        message=f"Deleted rule {rule_id}"
        + (" and its events." if purge else "; its events are kept.")
    )


SPEC = ToolSpec("sentinel_update", SCHEMA, "✏", run)
