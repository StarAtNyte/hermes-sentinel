"""Event routes: list with filters, and feedback on an alert."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Request

from ..common import access
from ..common.http import (
    ApiError,
    check_event_id,
    check_rule_id,
    fail,
    positive_int,
    read_body,
    reply,
    run_blocking,
)
from ..common.masking import mask
from ..common.shapes import call_tool, event_item

router = APIRouter()

__all__ = ["router"]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_VERDICTS = ("correct", "false_alarm")


def _limit(limit: str | None) -> int:
    if limit in (None, ""):
        return DEFAULT_LIMIT
    value = positive_int(limit)
    if value is None:
        raise ApiError(400, "limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _since(since: str | None) -> Any:
    if not since:
        return None
    if len(since) > 64:
        raise ApiError(400, "invalid 'since'")
    try:
        return access.module("agent.tools").parse_time_bound(since)
    except Exception as exc:  # noqa: BLE001 - ToolError
        raise ApiError(400, mask(str(exc))) from None


def _list_events(limit: str | None, rule_id: str | None, since: str | None) -> list[dict]:
    n = _limit(limit)
    rid = check_rule_id(rule_id) if rule_id else None
    bound = _since(since)
    live = access.sentinel()
    if live is None:
        return []
    store = live.store
    events = store.query_events(
        rule_id=rid, since=bound.astimezone(UTC) if bound else None, limit=n
    )
    rule_map = {spec.id: spec for spec in store.list_rules()}
    return [event_item(e, rule_map) for e in events]


def _feedback(event_id: str, data: dict[str, Any]) -> dict[str, Any]:
    eid = check_event_id(event_id)
    verdict = data.get("verdict")
    if verdict not in _VERDICTS:
        raise ApiError(400, "verdict must be 'correct' or 'false_alarm'")
    live = access.need()
    result = call_tool(live, "sentinel_feedback", {"event_id": eid, "verdict": verdict})
    store = live.store
    event = store.get_event(eid)
    if event is None:
        raise ApiError(404, f"no event #{eid}")
    rule = store.get_rule(event.rule_id)
    threshold = result.get("confidence_threshold")
    if threshold is None and rule is not None:
        threshold = rule.confidence_threshold
    rules_map = {rule.id: rule} if rule is not None else {}
    return {
        "ok": True,
        "event": event_item(event, rules_map),
        "threshold": float(threshold) if threshold is not None else 0.0,
    }


@router.get("/events")
async def events(
    limit: str | None = None, rule_id: str | None = None, since: str | None = None
) -> Any:
    """Recent events, newest first, optionally for one rule and since a time."""
    return reply(await run_blocking(_list_events, limit, rule_id, since))


@router.post("/events/{event_id}/feedback")
async def feedback(event_id: str, request: Request) -> Any:
    """Mark an event correct or a false alarm."""
    try:
        data = await read_body(request)
    except ApiError as exc:
        return fail(exc.status, exc.message)
    return reply(await run_blocking(_feedback, event_id, data))
