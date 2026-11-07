"""Chat-session matching helpers: effective origins and "run /sentinel here" hints.

A tool call in a messaging chat carries no routing key, so a rule created by the agent only
knows its ``session_id``. When ``/sentinel`` later ran in that session, the :class:`RouteBook`
learned the chat; this module joins the two so the chat keeps seeing its own rules, without
ever showing a rule to a chat it cannot be tied to.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .route_book import RouteBook
from .routing import LOCAL_SURFACES, clean_value, is_chat_origin, same_chat


def has_route(origin: Mapping[str, Any] | None) -> bool:
    """True when ``origin`` names a session key or a messaging chat to deliver to."""
    origin = origin or {}
    if clean_value(origin.get("session_key")):
        return True
    platform = (clean_value(origin.get("platform")) or "").lower()
    return bool(platform and platform not in LOCAL_SURFACES and clean_value(origin.get("chat_id")))


def effective_origin(origin: Mapping[str, Any], routes: RouteBook | None) -> dict[str, Any]:
    """``origin`` completed with the route learned for its ``session_id`` (stored one wins)."""
    merged = dict(origin)
    session_id = clean_value(merged.get("session_id"))
    if routes is None or not session_id:
        return merged
    for key, value in routes.lookup(session_id).items():
        merged.setdefault(key, value)
    return merged


def visible_to(
    rule_origin: Mapping[str, Any], caller: Mapping[str, Any], routes: RouteBook | None
) -> bool:
    """Whether the caller may see a rule: ``same_chat`` on the route-completed origin."""
    return same_chat(effective_origin(rule_origin, routes), caller)


def route_hint(rule_origin: Mapping[str, Any], caller: Mapping[str, Any]) -> str:
    """Hint for a chat caller whose visible rule has no stored route, else an empty string."""
    if not is_chat_origin(caller) or has_route(rule_origin):
        return ""
    same_session = clean_value(rule_origin.get("session_id")) == clean_value(
        caller.get("session_id")
    )
    where = "in this session" if same_session else "in an earlier session of this chat"
    return (
        f"this rule was created {where} and its alerts do not know this chat yet - "
        "run /sentinel here"
    )
