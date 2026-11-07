"""Rule routes: list, pause, resume, delete and create (``POST /watch``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..common import access
from ..common.http import (
    MAX_SOURCE,
    MAX_TEXT,
    ApiError,
    check_rule_id,
    fail,
    opt_str,
    read_body,
    reply,
    run_blocking,
)
from ..common.masking import mask
from ..common.shapes import call_tool, known_rule, rule_item

router = APIRouter()

__all__ = ["router"]


def _list_rules() -> list[dict[str, Any]]:
    live = access.sentinel()
    if live is None:
        return []
    return [rule_item(live, spec) for spec in live.store.list_rules()]


def _update_rule(rule_id: str, action: str) -> dict[str, Any]:
    check_rule_id(rule_id)
    live = access.need()
    spec = known_rule(live, rule_id)
    if action == "delete":
        before = rule_item(live, spec)
        call_tool(live, "sentinel_update", {"rule_id": rule_id, "action": "delete"})
        return {"ok": True, "rule": before}
    call_tool(live, "sentinel_update", {"rule_id": rule_id, "action": action})
    return {"ok": True, "rule": rule_item(live, known_rule(live, rule_id))}


def _watch(data: dict[str, Any]) -> dict[str, Any]:
    text = opt_str(data, "text", MAX_TEXT)
    source = opt_str(data, "source", MAX_SOURCE)
    recipe = opt_str(data, "recipe", 64)
    if not text and not recipe:
        raise ApiError(400, "say what to watch for in 'text' (or pick a 'recipe')")
    live = access.need()
    args: dict[str, Any] = {}
    if text:
        args["what"] = text
    if source:
        args["source"] = source
    if recipe:
        args["recipe"] = recipe
    result = call_tool(live, "sentinel_watch", args)
    saved = known_rule(live, result["rule"]["rule_id"])
    out: dict[str, Any] = {"ok": True, "rule": rule_item(live, saved)}
    note = str(result.get("message") or "").split(" Alerts are injected")[0].strip()
    if note:
        out["note"] = mask(note)
    return out


@router.get("/rules")
async def rules() -> Any:
    """Every stored rule with its live status."""
    return reply(await run_blocking(_list_rules))


@router.post("/rules/{rule_id}/pause")
async def pause_rule(rule_id: str) -> Any:
    """Pause one rule."""
    return reply(await run_blocking(_update_rule, rule_id, "pause"))


@router.post("/rules/{rule_id}/resume")
async def resume_rule(rule_id: str) -> Any:
    """Resume one rule."""
    return reply(await run_blocking(_update_rule, rule_id, "resume"))


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str) -> Any:
    """Delete one rule (its events are kept)."""
    return reply(await run_blocking(_update_rule, rule_id, "delete"))


@router.post("/watch")
async def watch(request: Request) -> Any:
    """Create a rule from plain text or a recipe."""
    try:
        data = await read_body(request)
    except ApiError as exc:
        return fail(exc.status, exc.message)
    return reply(await run_blocking(_watch, data))
