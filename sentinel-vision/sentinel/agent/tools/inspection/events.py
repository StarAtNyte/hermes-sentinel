"""``sentinel_events``: search confirmed events by text, rule, time range and feedback."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from typing import Any

from ....alerts import is_chat_origin, visible_to
from ..common import (
    ToolContext,
    ToolError,
    ToolSpec,
    event_dict,
    int_arg,
    ok,
    parse_time_bound,
    schema,
    str_arg,
)

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_events",
    "Search what Sentinel saw: confirmed events with captions, times and photos. Combine "
    "free text with a time range, e.g. text='door', since='today'.",
    {
        "text": {"type": "string", "description": "Words to search captions for."},
        "rule_id": {"type": "string"},
        "since": {
            "type": "string",
            "description": "ISO-8601 time, 'today', 'yesterday', or '2h' / '3d' ago.",
        },
        "until": {"type": "string", "description": "Same formats as since (exclusive)."},
        "feedback": {"type": "string", "enum": ["correct", "false_alarm"]},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Max events (default 20).",
        },
    },
)


def _feedback_filter(args: Mapping[str, Any]) -> str | None:
    feedback = str_arg(args, "feedback", limit=16) or None
    if feedback is not None and feedback not in ("correct", "false_alarm"):
        raise ToolError("feedback must be 'correct' or 'false_alarm'")
    return feedback


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Events matching the filters; messaging chats only see events of their own rules."""
    text = str_arg(args, "text", limit=500)
    rule_id = str_arg(args, "rule_id", limit=64) or None
    feedback = _feedback_filter(args)
    limit = max(1, min(int_arg(args, "limit") or 20, 100))
    since = parse_time_bound(args.get("since"))
    until = parse_time_bound(args.get("until"))
    sentinel = context.sentinel
    store = sentinel.store
    caller = context.caller(kwargs)
    scoped = is_chat_origin(caller)
    if rule_id and scoped:
        context.rule(rule_id, caller)  # not this chat's rule: "no rule"
    events = store.query_events(
        text=text or None,
        rule_id=rule_id,
        since=since.astimezone(UTC) if since else None,
        until=until.astimezone(UTC) if until else None,
        feedback=feedback,
        limit=limit,
    )
    rules = {s.id: s for s in store.list_rules()}
    if scoped:  # messaging chats only see events of their own rules
        events = [
            e
            for e in events
            if e.rule_id in rules and visible_to(rules[e.rule_id].origin, caller, sentinel.routes)
        ]
    items = [event_dict(e, rules.get(e.rule_id)) for e in events]
    summary = "; ".join(f"{i['time'][11:16]} {i['caption']}" for i in items[:10])
    return ok(count=len(items), events=items, summary=summary or "No matching events.")


SPEC = ToolSpec("sentinel_events", SCHEMA, "\U0001f5c2", run)
