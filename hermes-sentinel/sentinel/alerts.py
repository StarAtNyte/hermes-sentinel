"""Alert delivery: wake the agent in the chat that created a rule.

When a rule fires, :class:`Alerter` sends a compact message (caption, confidence, event id,
``MEDIA:<snapshot>`` and, for ``action=agent`` rules, the action prompt) with
``ctx.inject_message`` so the agent relays it - with the photo - in the right conversation.

Routing uses the origin recorded on the rule when it was created (:func:`capture_origin`).
Routes are tried in order until the host accepts one:

1. a known ``session_key`` -> ``inject_message(text, session_key=...)``;
2. a messaging ``platform`` + ``chat_id`` -> ``inject_message(text, origin={...})``;
3. a local session (CLI, TUI, Desktop): its ``session_id`` is its durable session key;
4. a key-less ``inject_message(text)`` (the host picks the local CLI session, if any).

If injection is refused (``False``) or raises, ``ctx.post_message`` is tried. Nothing here
waits for a human or blocks on the network (catalog rule 12): both host calls only queue.

Rate limits: at most one alert per rule every ``per_rule_min_interval`` seconds and
``global_per_hour`` alerts per rolling hour; notices (source offline, budget) have their own
hourly cap. Suppressed alerts are counted and mentioned in the next delivered one; the events
themselves are always stored.

How the origin is learned (public APIs only)
--------------------------------------------
Hermes passes tool handlers ``task_id``, ``session_id`` and ``user_task`` keywords, and
``ctx.session_surface()`` names the calling client or platform. That is enough for local
sessions. A messaging chat's routing key is only handed to slash-command handlers (documented
``session_key`` / ``platform`` / ``source`` keywords), so ``/sentinel`` remembers it per
``session_id`` in a :class:`RouteBook`; ``sentinel_watch`` then finds it, and ``/sentinel
here`` re-binds existing rules to the chat it is typed in. A background thread does not
inherit the calling context, which is why the origin is captured inside the tool call and
stored on the rule.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "LOCAL_SURFACES",
    "Alerter",
    "Delivery",
    "RouteBook",
    "capture_origin",
    "format_event_message",
    "route_from_kwargs",
]

#: Session surfaces that are not a messaging chat (an ``origin`` route makes no sense there).
LOCAL_SURFACES = frozenset({"", "cli", "tui", "desktop", "local", "acp", "api_server", "gateway"})
#: Origin keys forwarded to ``inject_message(origin=...)`` (``SessionSource.to_dict`` shape).
_ROUTE_KEYS = ("platform", "chat_id", "chat_type", "chat_name", "thread_id", "user_id",
               "user_name")
MAX_ORIGIN_VALUE = 500
MAX_MESSAGE_LEN = 4000


def _clean_value(value: Any) -> str | None:
    value = getattr(value, "value", value)  # enums (e.g. a Platform) -> their value
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_ORIGIN_VALUE:
        return None
    return text


def route_from_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, str]:
    """Routing fields from handler keywords (``session_key``, ``platform``, ``source``...)."""
    kwargs = kwargs or {}
    out: dict[str, str] = {}
    source = kwargs.get("source")
    for key in ("session_key", *_ROUTE_KEYS):
        value = _clean_value(kwargs.get(key))
        if value is None and source is not None and key in _ROUTE_KEYS:
            value = _clean_value(
                source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
            )
        if value is not None:
            out[key] = value
    return out


def capture_origin(
    kwargs: Mapping[str, Any] | None,
    ctx: Any = None,
    *,
    routes: RouteBook | None = None,
) -> dict[str, Any]:
    """Where alerts for a rule created in this call should go.

    Reads the handler keywords (``session_id``, plus ``session_key`` / ``platform`` /
    ``source`` when the host passes them), ``ctx.session_surface()``, and a route remembered
    for this ``session_id`` by ``/sentinel``. Returns only non-empty scalar values (a valid
    ``RuleSpec.origin``). Never raises.
    """
    kwargs = kwargs or {}
    out: dict[str, Any] = dict(route_from_kwargs(kwargs))
    session_id = _clean_value(kwargs.get("session_id"))
    if session_id:
        out["session_id"] = session_id
    if ctx is not None:
        try:
            surface = _clean_value(ctx.session_surface())
        except Exception:  # noqa: BLE001
            surface = None
        if surface:
            out["surface"] = surface
    if routes is not None and session_id and "session_key" not in out:
        for key, value in routes.lookup(session_id).items():
            out.setdefault(key, value)
    return out


class RouteBook:
    """Bounded, thread-safe map ``session_id -> route`` learned from slash commands.

    Persisted as JSON at ``path`` (when given) so a route survives restarts. Never raises.
    """

    def __init__(self, path: Path | str | None = None, *, limit: int = 256) -> None:
        self._path = Path(path) if path is not None else None
        self._limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._routes: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("sentinel: ignoring unreadable routes file: %s", exc)
            return
        if not isinstance(data, dict):
            return
        for session_id, route in list(data.items())[-self._limit:]:
            if isinstance(session_id, str) and isinstance(route, dict):
                clean = route_from_kwargs(route)
                if clean:
                    self._routes[session_id] = clean

    def _save_locked(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._routes), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("sentinel: could not save routes: %s", exc)

    def remember(self, kwargs: Mapping[str, Any] | None) -> dict[str, str]:
        """Record the route of a slash-command call; returns it (empty if none)."""
        route = route_from_kwargs(kwargs)
        session_id = _clean_value((kwargs or {}).get("session_id")) or route.get("session_key")
        if not session_id or "session_key" not in route:
            return route
        with self._lock:
            if self._routes.get(session_id) != route:
                self._routes[session_id] = route
                self._routes.move_to_end(session_id)
                while len(self._routes) > self._limit:
                    self._routes.popitem(last=False)
                self._save_locked()
        return route

    def lookup(self, session_id: str | None) -> dict[str, str]:
        if not session_id:
            return {}
        with self._lock:
            return dict(self._routes.get(session_id, {}))


@dataclass(frozen=True)
class Delivery:
    """Outcome of one delivery attempt.

    ``via``: ``session_key`` / ``origin`` / ``keyless`` (injected), ``post`` (posted),
    ``suppressed`` (rate limit) or ``failed``.
    """

    ok: bool
    via: str
    detail: str = ""


def _local_time(ts: str) -> str:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(ts)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def format_event_message(
    rule: Any,
    event: Any,
    *,
    source_name: str,
    include_media: bool = True,
    suppressed: int = 0,
) -> str:
    """The text injected for an event (``rule``: ``RuleSpec``, ``event``: ``EventRecord``)."""
    conf = event.confidence
    conf_text = f" (confidence {conf:.2f})" if isinstance(conf, (int, float)) else ""
    caption = (event.caption or "").strip() or "the watch condition was seen"
    what = (rule.text or rule.question or "").strip()
    lines = [
        f"[Sentinel alert] {caption}{conf_text}",
        f'Rule {rule.id} ("{what[:200]}") on {source_name}. Event #{event.id} at '
        f"{_local_time(event.ts)}.",
    ]
    if event.reason:
        lines.append(f"Evidence: {event.reason[:300]}")
    if suppressed:
        lines.append(f"({suppressed} earlier alert(s) were rate-limited; see sentinel_events.)")
    if include_media and event.snapshot_path:
        lines.append(f"MEDIA:{event.snapshot_path}")
    lines.append(
        "Tell the user about this now"
        + (", attaching the photo" if include_media and event.snapshot_path else "")
        + f'. If they say it is wrong, call sentinel_feedback with event_id={event.id} and '
        'verdict "false_alarm" (or "correct").'
    )
    if getattr(rule, "action", "notify") == "agent" and getattr(rule, "action_prompt", ""):
        lines.append(f"Then carry out the rule's action: {rule.action_prompt}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE_LEN else text[: MAX_MESSAGE_LEN - 1] + "…"


class _Window:
    """Sliding one-hour window of send times (bounded by the cap)."""

    def __init__(self, cap: int) -> None:
        self.cap = max(0, int(cap))
        self._times: deque[float] = deque(maxlen=max(1, self.cap))

    def allow(self, now: float) -> bool:
        while self._times and now - self._times[0] >= 3600.0:
            self._times.popleft()
        if self.cap <= 0 or len(self._times) >= self.cap:
            return False
        self._times.append(now)
        return True


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
        self._ctx = ctx
        self._clock = clock
        self.per_rule_min_interval = max(0.0, float(per_rule_min_interval))
        self.include_media = bool(include_media)
        self._lock = threading.Lock()
        self._alerts = _Window(global_per_hour)
        self._notices = _Window(notices_per_hour)
        self._last_by_rule: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self.sent = 0
        self.suppressed_total = 0
        self.failed = 0

    # ---- routing ----------------------------------------------------------------------

    def _routes(self, origin: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Injection attempts for ``origin``, most specific first."""
        attempts: list[tuple[str, dict[str, Any]]] = []
        session_key = _clean_value(origin.get("session_key"))
        platform = (_clean_value(origin.get("platform")) or "").lower()
        surface = (_clean_value(origin.get("surface")) or platform).lower()
        chat_id = _clean_value(origin.get("chat_id"))
        session_id = _clean_value(origin.get("session_id"))
        if session_key:
            attempts.append(("session_key", {"session_key": session_key}))
        if platform and chat_id and platform not in LOCAL_SURFACES:
            route = {k: origin[k] for k in _ROUTE_KEYS if _clean_value(origin.get(k))}
            attempts.append(("origin", {"origin": route}))
        if session_id and session_id != session_key and surface in LOCAL_SURFACES:
            attempts.append(("session_id", {"session_key": session_id}))
        attempts.append(("keyless", {}))
        return attempts

    def _deliver(self, text: str, origin: Mapping[str, Any] | None) -> Delivery:
        for via, kwargs in self._routes(origin or {}):
            try:
                if self._ctx.inject_message(text, role="user", **kwargs):
                    return Delivery(True, via)
            except Exception as exc:  # noqa: BLE001 - delivery must never raise
                logger.warning("sentinel: inject_message (%s) failed: %s", via, exc)
        try:
            handle = self._ctx.post_message(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentinel: post_message failed: %s", exc)
            return Delivery(False, "failed", str(exc)[:200])
        if handle is not None and getattr(handle, "ok", True):
            return Delivery(True, "post")
        error = getattr(handle, "error", None) or "no conversation to deliver to"
        logger.warning("sentinel: alert could not be delivered: %s", error)
        return Delivery(False, "failed", str(error)[:200])

    # ---- public API -------------------------------------------------------------------

    def alert_event(
        self, rule: Any, event: Any, *, source_name: str, include_media: bool | None = None
    ) -> Delivery:
        """Deliver the alert for a stored event, subject to the rate limits."""
        now = self._clock()
        rule_id = str(rule.id)
        with self._lock:
            last = self._last_by_rule.get(rule_id)
            if last is not None and now - last < self.per_rule_min_interval:
                limited = True
            else:
                limited = not self._alerts.allow(now)
            if limited:
                self._suppressed[rule_id] = self._suppressed.get(rule_id, 0) + 1
                self.suppressed_total += 1
                return Delivery(False, "suppressed", "rate limited")
            self._last_by_rule[rule_id] = now
            if len(self._last_by_rule) > 4096:  # bounded: forget the oldest entries
                for key in sorted(self._last_by_rule, key=self._last_by_rule.get)[:1024]:
                    self._last_by_rule.pop(key, None)
            suppressed = self._suppressed.pop(rule_id, 0)
        media = self.include_media if include_media is None else include_media
        try:
            text = format_event_message(
                rule, event, source_name=source_name, include_media=media, suppressed=suppressed
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentinel: could not format alert: %s", exc)
            text = f"[Sentinel alert] rule {rule_id} fired (event #{getattr(event, 'id', '?')})."
        result = self._deliver(text, getattr(rule, "origin", None))
        with self._lock:
            if result.ok:
                self.sent += 1
            else:
                self.failed += 1
        return result

    def notice(self, text: str, *, origin: Mapping[str, Any] | None = None) -> Delivery:
        """Deliver a system notice (source offline/online, budget, judge errors)."""
        with self._lock:
            if not self._notices.allow(self._clock()):
                self.suppressed_total += 1
                return Delivery(False, "suppressed", "notice rate limited")
        body = f"[Sentinel notice] {text.strip()}"[:MAX_MESSAGE_LEN]
        body += "\nLet the user know briefly; no action is needed unless they ask."
        result = self._deliver(body, origin)
        with self._lock:
            if result.ok:
                self.sent += 1
            else:
                self.failed += 1
        return result

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "sent": self.sent,
                "suppressed": self.suppressed_total,
                "failed": self.failed,
            }
