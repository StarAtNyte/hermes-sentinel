"""Check that the calling chat is known, so alerts can find their way back."""

from __future__ import annotations

from ..env import SetupEnv
from ..model import OK, WARN, Check

__all__ = ["check_delivery"]


def check_delivery(env: SetupEnv) -> Check:
    """OK for a known chat or no session at all; WARN when the session has no route."""
    title = "Alert destination"
    if env.route is None:
        return Check(
            "delivery",
            title,
            OK,
            "Not tied to a session here. Run /sentinel setup in the chat that should "
            "get alerts to check it.",
        )
    if env.route.get("session_key"):
        return Check("delivery", title, OK, "This chat is known; alerts will come back here.")
    return Check(
        "delivery",
        title,
        WARN,
        "This session did not identify its chat, so alerts may not find their way back.",
        "Send /sentinel here in the chat that should receive alerts.",
    )
