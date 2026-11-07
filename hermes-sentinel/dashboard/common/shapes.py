"""JSON shapes of rules and events, and the bridge to the ``SentinelTools`` handlers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import access
from .http import MAX_CAPTION, MAX_TEXT, ApiError
from .masking import mask

__all__ = ["call_tool", "event_item", "iso", "known_rule", "rule_item"]


def iso(ts: float | None) -> str | None:
    """A unix time as an ISO-8601 UTC string, ``None`` for a missing time."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def rule_item(live: Any, spec: Any) -> dict[str, Any]:
    """A rule as the dashboard shows it, with credentials masked."""
    store = live.store
    engine = live.engine_if_built
    status = engine.rule_status(spec.id) if engine is not None else None
    text = access.tools(live).status_text(spec, engine, status)
    last = store.last_event(spec.id)
    hour, _day = store.usage_totals(rule_id=spec.id)
    return {
        "id": spec.id,
        "text": (spec.text or spec.question or "")[:MAX_TEXT],
        "source": mask(spec.source),
        "mode": spec.mode,
        "status": mask(text),
        "paused": not spec.enabled,
        "last_event_at": last.ts if last else None,
        "vision_calls_hour": hour,
        "cooldown_s": spec.cooldown_seconds,
        "hold_s": spec.hold_seconds,
    }


def event_item(event: Any, rules: dict[str, Any]) -> dict[str, Any]:
    """An event as the dashboard shows it (the photo is fetched separately)."""
    rule = rules.get(event.rule_id)
    path = event.snapshot_path
    return {
        "id": event.id,
        "rule_id": event.rule_id,
        "rule_text": ((rule.text or rule.question) if rule else "")[:MAX_TEXT],
        "ts": event.ts,
        "caption": (event.caption or "")[:MAX_CAPTION],
        "confidence": round(event.confidence, 3) if event.confidence is not None else None,
        "feedback": event.feedback,
        "has_snapshot": bool(path) and Path(path).is_file(),
    }


def call_tool(live: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a SentinelTools handler; raise :class:`ApiError` with a fitting status on failure."""
    result = json.loads(access.tools(live).handlers()[name](args))
    if not result.get("ok"):
        message = mask(result.get("error") or "request failed")
        low = message.lower()
        if low.startswith(("no rule", "no event")):
            raise ApiError(404, message)
        if "limited to" in low:
            raise ApiError(429, message)
        if low.startswith(("sentinel storage", "unexpected")):
            raise ApiError(503 if "storage" in low else 500, message)
        raise ApiError(400, message)
    return result


def known_rule(live: Any, rule_id: str) -> Any:
    """The stored rule ``rule_id``, or a 404 :class:`ApiError`."""
    spec = live.store.get_rule(rule_id)
    if spec is None:
        raise ApiError(404, f"no rule with id {rule_id!r}")
    return spec
