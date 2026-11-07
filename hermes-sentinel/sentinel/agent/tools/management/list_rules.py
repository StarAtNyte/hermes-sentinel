"""``sentinel_rules``: list watch rules with their status, last event and usage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....alerts import route_hint, visible_to
from ....rules import RuleSpec
from ..common import ToolContext, ToolSpec, event_dict, ok, rule_brief, schema, str_arg

__all__ = ["SPEC", "status_text"]

SCHEMA = schema(
    "sentinel_rules",
    "List watch rules with their status (watching, paused, source offline), last event and "
    "vision checks used. A 'hint' on a rule means its alerts do not know this chat yet "
    "(tell the user to send /sentinel here).",
    {"rule_id": {"type": "string", "description": "Show only this rule."}},
)


def status_text(spec: RuleSpec, engine: Any, status: Mapping[str, Any] | None) -> str:
    """Human status of a rule: paused, not running, starting, source offline, or state."""
    if not spec.enabled:
        return "paused"
    if status is None or not engine.running:
        return "starting" if engine and engine.running else "not running"
    if status.get("source_online") is False:
        return f"source offline: {status.get('source_error') or 'unknown'}"
    return str(status.get("state", "idle"))


def _rule_entry(
    context: ToolContext, spec: RuleSpec, caller: Mapping[str, Any], engine: Any
) -> dict[str, Any]:
    store = context.sentinel.store
    entry = rule_brief(spec)
    hint = route_hint(spec.origin, caller)
    if hint:
        entry["hint"] = hint
    status = engine.rule_status(spec.id) if engine is not None else None
    entry["status"] = status_text(spec, engine, status)
    if status:
        entry["checks_since_load"] = status.get("checks")
        entry["last_checked"] = status.get("last_checked")
        verdict = status.get("last_verdict") or {}
        if verdict.get("error"):
            entry["last_error"] = verdict["error"]
    hour, day = store.usage_totals(rule_id=spec.id)
    entry["vision_checks"] = {"this_hour": hour, "last_24h": day}
    last = store.last_event(spec.id)
    entry["last_event"] = event_dict(last) if last else None
    entry["events_total"] = store.count_events(rule_id=spec.id)
    return entry


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """List the rules the caller can see (or one rule) with live status."""
    rule_id = str_arg(args, "rule_id", limit=64)
    sentinel = context.sentinel
    caller = context.caller(kwargs)
    specs = (
        [context.rule(rule_id, caller)]
        if rule_id
        else [
            r for r in sentinel.store.list_rules() if visible_to(r.origin, caller, sentinel.routes)
        ]
    )
    engine = sentinel.engine_if_built
    out = [_rule_entry(context, spec, caller, engine) for spec in specs]
    extra: dict[str, Any] = {}
    blocked = sentinel.alerter.delivery_blocked()
    if blocked:
        extra["delivery_blocked"] = blocked
    return ok(rules=out, count=len(out), budget=sentinel.budget.status().describe(), **extra)


SPEC = ToolSpec("sentinel_rules", SCHEMA, "\U0001f4cb", run)
