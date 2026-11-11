"""Usage text and the caller helpers shared by every ``/sentinel`` subcommand."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ...alerts import (
    adopt_session_rules,
    capture_origin,
    delivery_label,
    describe_held,
    visible_to,
)
from ...runtime import Sentinel

logger = logging.getLogger(__name__)

__all__ = [
    "ARGS_HINT",
    "USAGE",
    "adopt_route",
    "caller_origin",
    "delivery_lines",
    "held_alert_lines",
    "remember_route",
    "take_held_alert",
    "visible_rules",
]

ARGS_HINT = "[setup|status|list|pause <id>|resume <id>|test <id>|budget|here [<id>|all]]"
USAGE = (
    "Usage: /sentinel setup [--test-delivery] | status | list | pause <rule_id> | "
    "resume <rule_id> | test <rule_id> | budget | here [<rule_id>|all]"
)


def remember_route(sentinel: Sentinel, kwargs: dict[str, Any]) -> dict[str, str]:
    """Learn this chat's route (documented command keywords) for later rules."""
    try:
        return sentinel.routes.remember(kwargs)
    except Exception as exc:  # noqa: BLE001 - routing memory is best effort
        logger.debug("sentinel: could not remember route: %s", exc)
        return {}


def adopt_route(sentinel: Sentinel, route: dict[str, str], kwargs: dict[str, Any]) -> None:
    """Give this session's route-less rules the route just learned (any subcommand)."""
    session_id = str(kwargs.get("session_id") or "")
    try:
        adopt_session_rules(
            sentinel.store, sentinel.engine_if_built, sentinel.store.list_rules(), session_id, route
        )
    except Exception as exc:  # noqa: BLE001 - adoption is best effort
        logger.debug("sentinel: could not adopt session rules: %s", exc)


def caller_origin(sentinel: Sentinel, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Who is typing: the command keywords, the session surface and any known route."""
    try:
        return capture_origin(kwargs, sentinel.ctx, routes=sentinel.routes)
    except Exception as exc:  # noqa: BLE001 - broad: reads host-supplied context and kwargs
        logger.debug("sentinel: could not capture the caller origin: %s", type(exc).__name__)
        return {}


def visible_rules(sentinel: Sentinel, caller: dict[str, Any]) -> list[Any]:
    """Rules the caller may see: all for local sessions, only its own in a messaging chat."""
    routes = sentinel.routes
    return [r for r in sentinel.store.list_rules() if visible_to(r.origin, caller, routes)]


def held_alert_lines(sentinel: Sentinel, rules: Iterable[Any]) -> list[str]:
    """One line per rule in ``rules`` that has an undelivered alert."""
    book = sentinel.held_alerts
    lines = []
    for spec in rules:
        held = book.get(spec.id)
        if held is not None:
            lines.append(describe_held(spec.id, held))
    return lines


def delivery_lines(sentinel: Sentinel, rules: Iterable[Any]) -> list[str]:
    """One line per rule in ``rules`` whose newest event has a recorded alert outcome."""
    lines = []
    for spec in rules:
        last = sentinel.store.last_event(spec.id)
        text = last and delivery_label(last.delivery, last.delivery_detail, last.delivery_ts)
        if text:
            lines.append(f"rule {spec.id}: last alert (event #{last.id}) {text}")
    return lines


def take_held_alert(sentinel: Sentinel, rule_id: str) -> str:
    """Clear the held alert of ``rule_id``; returns a sentence about it, or empty if none."""
    book = sentinel.held_alerts
    held = book.get(rule_id)
    if held is None:
        return ""
    book.clear(rule_id)
    return f" An alert was held while it had no live chat (event #{held.event_id})."
