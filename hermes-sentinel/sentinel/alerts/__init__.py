"""Alert delivery: wake the agent in the chat that created a rule.

When a rule fires, :class:`Alerter` sends a compact message (caption, confidence, event id,
``MEDIA:<snapshot>`` and, for ``action=agent`` rules, the action prompt) with
``ctx.inject_message`` so the agent relays it in the right conversation. Routes (session key,
messaging origin, local session, key-less) are tried in order; chat origins never reach the
local CLI. Nothing here waits for a human (catalog rule 12). Model-written text is fenced as
an untrusted observation. Rate limits: per rule, per hour, and a separate notice cap.
"""

from .delivery import Alerter, Delivery
from .formatting import (
    BLOCKED_TEXT,
    GATEWAY_INJECTION_YAML,
    MAX_CAPTION_LEN,
    MAX_MESSAGE_LEN,
    MAX_REASON_LEN,
    format_event_message,
    sanitize_untrusted,
)
from .rate_limit import HourlyWindow
from .route_book import RouteBook
from .routing import (
    LOCAL_SURFACES,
    MAX_ORIGIN_VALUE,
    capture_origin,
    is_chat_origin,
    route_from_kwargs,
    same_chat,
)

__all__ = [
    "BLOCKED_TEXT",
    "GATEWAY_INJECTION_YAML",
    "LOCAL_SURFACES",
    "MAX_CAPTION_LEN",
    "MAX_MESSAGE_LEN",
    "MAX_ORIGIN_VALUE",
    "MAX_REASON_LEN",
    "Alerter",
    "Delivery",
    "HourlyWindow",
    "RouteBook",
    "capture_origin",
    "format_event_message",
    "is_chat_origin",
    "route_from_kwargs",
    "same_chat",
    "sanitize_untrusted",
]
