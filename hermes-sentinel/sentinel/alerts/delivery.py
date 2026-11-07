"""Alerter: rate-limited delivery of alerts and notices through the plugin context."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .formatting import BLOCKED_TEXT, MAX_MESSAGE_LEN, format_event_message
from .rate_limit import HourlyWindow
from .routing import LOCAL_SURFACES, ROUTE_KEYS, clean_value, is_chat_origin

logger = logging.getLogger(__name__)

#: Least time between two delivery self-tests (each one starts an agent turn).
TEST_MIN_INTERVAL_SECONDS = 60.0
_MAX_TRACKED_RULES = 4096
_FORGET_RULES = 1024


@dataclass(frozen=True)
class Delivery:
    """Outcome of one delivery attempt.

    ``via``: ``session_key`` / ``origin`` / ``keyless`` (injected), ``post`` (posted),
    ``suppressed`` (rate limit) or ``failed``.
    """

    ok: bool
    via: str
    detail: str = ""


class Alerter:
    """Delivers alerts and notices through the plugin context. Thread-safe; never raises."""

    def __init__(
        self,
        ctx: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        per_rule_min_interval: float = 30.0,
        global_per_hour: int = 60,
        notices_per_hour: int = 30,
        include_media: bool = True,
    ) -> None:
        """Bind to ``ctx`` (needs ``inject_message``; optionally ``post_message``)."""
        self._ctx = ctx
        self._clock = clock
        self.per_rule_min_interval = max(0.0, float(per_rule_min_interval))
        self.include_media = bool(include_media)
        # Guards the windows, per-rule bookkeeping, counters and the blocked flag.
        self._lock = threading.Lock()
        self._alerts = HourlyWindow(global_per_hour)
        self._notices = HourlyWindow(notices_per_hour)
        self._last_by_rule: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._last_test: float | None = None
        self.sent = 0
        self.suppressed_total = 0
        self.failed = 0
        self._blocked = False
        self._verified = False

    # ---- routing ----------------------------------------------------------------------

    def _routes(self, origin: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Injection attempts for ``origin``, most specific first.

        Chat origins never get the local routes (``session_id``, key-less): those reach the
        classic CLI, which is a different person's session.
        """
        attempts: list[tuple[str, dict[str, Any]]] = []
        session_key = clean_value(origin.get("session_key"))
        platform = (clean_value(origin.get("platform")) or "").lower()
        chat_id = clean_value(origin.get("chat_id"))
        session_id = clean_value(origin.get("session_id"))
        chat = is_chat_origin(origin)
        has_route = bool(platform and chat_id and platform not in LOCAL_SURFACES)
        # A classic CLI process puts any origin-less injection, keyed or not, into its local
        # REPL. So a chat origin with a full route uses only the origin attempt.
        if session_key and not (chat and has_route):
            attempts.append(("session_key", {"session_key": session_key}))
        if has_route:
            route = {k: origin[k] for k in ROUTE_KEYS if clean_value(origin.get(k))}
            attempts.append(("origin", {"origin": route}))
        if not chat:
            if session_id and session_id != session_key:
                attempts.append(("session_id", {"session_key": session_id}))
            attempts.append(("keyless", {}))
        return attempts

    def _try_post(self, text: str) -> tuple[Delivery | None, str]:
        """``ctx.post_message`` fallback: (delivery if it worked, failure detail)."""
        try:
            handle = self._ctx.post_message(text)
        except Exception as exc:  # noqa: BLE001 - broad: host messaging callable, reported as the failure detail
            logger.warning("sentinel: post_message failed: %s", exc)
            return None, str(exc)[:200]
        if handle is not None and getattr(handle, "ok", True):
            return Delivery(True, "post"), ""
        return None, str(getattr(handle, "error", None) or "no conversation to deliver to")

    def _deliver(
        self, text: str, origin: Mapping[str, Any] | None, *, post_fallback: bool = False
    ) -> Delivery:
        origin = origin or {}
        for via, kwargs in self._routes(origin):
            try:
                if self._ctx.inject_message(text, role="user", **kwargs):
                    self._set_blocked(False)
                    return Delivery(True, via)
            except Exception as exc:  # noqa: BLE001 - delivery must never raise
                logger.warning("sentinel: inject_message (%s) failed: %s", via, exc)
        detail = "no route accepted the message"
        if post_fallback and not is_chat_origin(origin):
            posted, detail = self._try_post(text)
            if posted is not None:
                return posted
        if not self._routes(origin):
            detail = "this chat has no known route; send /sentinel here in it"
        else:
            self._set_blocked(True)
        logger.warning("sentinel: alert could not be delivered: %s", detail)
        return Delivery(False, "failed", detail[:200])

    def _set_blocked(self, blocked: bool) -> None:
        with self._lock:
            self._blocked = blocked
            if blocked:
                self._verified = False

    def _count(self, result: Delivery) -> None:
        with self._lock:
            if result.ok:
                self.sent += 1
            else:
                self.failed += 1

    def delivery_blocked(self) -> str | None:
        """Sticky explanation while the last delivery failed on every route, else ``None``."""
        with self._lock:
            return BLOCKED_TEXT if self._blocked else None

    def delivery_verified(self) -> bool:
        """True after a delivery self-test succeeded and nothing has been refused since."""
        with self._lock:
            return self._verified

    def send_test(self, text: str, origin: Mapping[str, Any] | None) -> Delivery:
        """Send one self-test message to ``origin`` (not counted in stats).

        At most one test per ``TEST_MIN_INTERVAL_SECONDS``; a repeat inside the interval sends
        nothing and returns ``Delivery(False, "throttled", ...)``. Otherwise updates the sticky
        blocked flag like a real alert, and the verified flag.
        """
        now = self._clock()
        with self._lock:
            last = self._last_test
            if last is not None and 0 <= now - last < TEST_MIN_INTERVAL_SECONDS:
                return Delivery(False, "throttled", "already tested recently")
            self._last_test = now
        result = self._deliver(text, origin)
        with self._lock:
            self._verified = result.ok
        return result

    # ---- public API -------------------------------------------------------------------

    def _admit(self, rule_id: str, now: float) -> int | None:
        """Rate-limit check for an alert; the count of suppressed alerts to mention, or None."""
        with self._lock:
            last = self._last_by_rule.get(rule_id)
            if last is not None and now - last < self.per_rule_min_interval:
                limited = True
            else:
                limited = not self._alerts.allow(now)
            if limited:
                self._suppressed[rule_id] = self._suppressed.get(rule_id, 0) + 1
                self.suppressed_total += 1
                return None
            self._last_by_rule[rule_id] = now
            if len(self._last_by_rule) > _MAX_TRACKED_RULES:  # bounded: forget the oldest
                oldest = sorted(self._last_by_rule, key=self._last_by_rule.get)[:_FORGET_RULES]
                for key in oldest:
                    self._last_by_rule.pop(key, None)
            return self._suppressed.pop(rule_id, 0)

    def alert_event(
        self,
        rule: Any,
        event: Any,
        *,
        source_name: str,
        include_media: bool | None = None,
        post_fallback: bool = False,
    ) -> Delivery:
        """Deliver the alert for a stored event, subject to the rate limits.

        ``post_fallback``: also try ``ctx.post_message`` - only valid on a tool or command
        thread (a background thread has no session to post into).
        """
        rule_id = str(rule.id)
        suppressed = self._admit(rule_id, self._clock())
        if suppressed is None:
            return Delivery(False, "suppressed", "rate limited")
        media = self.include_media if include_media is None else include_media
        try:
            text = format_event_message(
                rule, event, source_name=source_name, include_media=media, suppressed=suppressed
            )
        except Exception as exc:  # noqa: BLE001 - broad: user rule text is formatted; fall back to a plain alert
            logger.warning("sentinel: could not format alert: %s", exc)
            text = f"[Sentinel alert] rule {rule_id} fired (event #{getattr(event, 'id', '?')})."
        result = self._deliver(text, getattr(rule, "origin", None), post_fallback=post_fallback)
        self._count(result)
        return result

    def notice(
        self, text: str, *, origin: Mapping[str, Any] | None = None, post_fallback: bool = False
    ) -> Delivery:
        """Deliver a system notice (source offline/online, budget, judge errors)."""
        with self._lock:
            if not self._notices.allow(self._clock()):
                self.suppressed_total += 1
                return Delivery(False, "suppressed", "notice rate limited")
        body = f"[Sentinel notice] {text.strip()}"[:MAX_MESSAGE_LEN]
        body += "\nLet the user know briefly; no action is needed unless they ask."
        result = self._deliver(body, origin, post_fallback=post_fallback)
        self._count(result)
        return result

    def stats(self) -> dict[str, int]:
        """Counters: alerts ``sent``, ``suppressed`` by rate limits, and ``failed``."""
        with self._lock:
            return {
                "sent": self.sent,
                "suppressed": self.suppressed_total,
                "failed": self.failed,
            }
