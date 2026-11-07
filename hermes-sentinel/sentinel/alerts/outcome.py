"""How one alert ended, in words for logs, status lines and tool output."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any


class AlertOutcome(StrEnum):
    """Final state of one alert delivery."""

    DELIVERED = "delivered"
    HELD = "held"
    REFUSED = "refused"
    NO_ROUTE = "no route"
    RATE_LIMITED = "rate limited"


#: Where a delivered alert went, by ``Delivery.via``.
_DESTINATIONS = {
    "session_key": "the session",
    "origin": "the chat",
    "session_id": "local session",
    "keyless": "local session",
    "post": "the conversation (posted)",
}


def destination(via: str) -> str:
    """Plain words for the route an alert was delivered through."""
    return _DESTINATIONS.get(via, via)


def log_line(rule_id: str, event_id: Any, outcome: AlertOutcome, reason: str) -> str:
    """The single log line for one alert."""
    verb = "to " + reason if outcome is AlertOutcome.DELIVERED else f"({reason})"
    return f"sentinel: alert for event {event_id} (rule {rule_id}) {outcome.value} {verb}"


def label(outcome: str | None, detail: str = "", ts: str | None = None) -> str | None:
    """``delivered 21:39:13`` or ``held: session not live`` for a stored outcome, else ``None``."""
    if not outcome:
        return None
    if outcome == AlertOutcome.DELIVERED.value:
        return f"delivered {_clock_time(ts)}".strip()
    return f"{outcome}: {detail}" if detail else outcome


def _clock_time(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return when.astimezone().strftime("%H:%M:%S")
