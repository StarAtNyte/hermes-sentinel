"""``/sentinel here``: route alerts of this session's rules to the current chat."""

from __future__ import annotations

from typing import Any

from ...alerts import bind_rules, plan_here
from ...runtime import Sentinel
from .common import visible_rules

__all__ = ["bind_here"]


def bind_here(
    sentinel: Sentinel,
    target: str,
    route: dict[str, str],
    kwargs: dict[str, Any],
    caller: dict[str, Any] | None = None,
) -> str:
    """Send alerts of this session's rules (or ``<id>`` / ``all``) to this chat.

    In a messaging chat only rules that already belong to that chat can be moved; a
    local session may move any rule.
    """
    if not route.get("session_key"):
        return (
            "This session did not identify itself (no session key), so alerts cannot "
            "be routed here."
        )
    caller = caller if caller is not None else {}
    session_id = str(kwargs.get("session_id") or route.get("session_key") or "")
    plan = plan_here(visible_rules(sentinel, caller), target, session_id)
    if not plan.rules:
        return plan.reply
    bound = bind_rules(sentinel.store, sentinel.engine_if_built, plan.rules, route)
    return f"Alerts for {', '.join(bound)} will now come to this chat."
