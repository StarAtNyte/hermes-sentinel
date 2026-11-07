"""Alert delivery: wake the agent in the chat that created a rule.

When a rule fires, :class:`Alerter` sends a compact message (caption, confidence, event id,
``MEDIA:<snapshot>`` and, for ``action=agent`` rules, the action prompt) with
``ctx.inject_message`` so the agent relays it in the right conversation. Routes (session key,
messaging origin, local session, key-less) are tried in order; chat origins never reach the
local CLI. Nothing here waits for a human (catalog rule 12). Model-written text is fenced as
an untrusted observation. Rate limits: per rule, per hour, and a separate notice cap.
"""

from .books import HeldAlert, HeldAlerts, RouteBook, describe_held
from .chat_hints import effective_origin, has_route, route_hint, visible_to
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
from .routing import (
    LOCAL_SURFACES,
    MAX_ORIGIN_VALUE,
    capture_origin,
    is_chat_origin,
    route_from_kwargs,
    same_chat,
)
from .rule_binding import HerePlan, adopt_session_rules, bind_rules, plan_here, rebound_origin
from .self_test import DeliveryTestResult, format_delivery_test, run_delivery_test

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
    "DeliveryTestResult",
    "HeldAlert",
    "HeldAlerts",
    "HerePlan",
    "HourlyWindow",
    "RouteBook",
    "adopt_session_rules",
    "bind_rules",
    "capture_origin",
    "describe_held",
    "effective_origin",
    "format_delivery_test",
    "format_event_message",
    "has_route",
    "is_chat_origin",
    "plan_here",
    "rebound_origin",
    "route_hint",
    "run_delivery_test",
    "route_from_kwargs",
    "same_chat",
    "sanitize_untrusted",
    "visible_to",
]
