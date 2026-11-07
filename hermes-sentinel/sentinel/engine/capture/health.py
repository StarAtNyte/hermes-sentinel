"""Source health: reconnect backoff, offline/back-online notices and who hears about them."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from ...sources import SecretScopeUnavailable
from ..infra import short_error
from ..models import SourceWorker
from ..state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["FINISHED_RETRY_S", "SECRET_SCOPE_RETRY_S", "SourceHealth"]

FINISHED_RETRY_S = 10 * 365 * 86400.0  # effectively never (finite, so status stays JSON-safe)
SECRET_SCOPE_RETRY_S = 5.0  # how often a camera waiting for the secret scope re-checks


class SourceHealth:
    """Tracks whether each source is up; decides when a notice goes out and to whom."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (settings, rules for notice routing, alerter)."""
        self._state = state

    def notify(self, rule_ids: list[str], notice: str) -> None:
        """Send ``notice`` once to every distinct chat that watches one of ``rule_ids``."""
        for origin in self._origins_for(rule_ids):
            self._state.alerter.notice(notice, origin=origin)

    def _origins_for(self, rule_ids: list[str]) -> list[dict[str, Any]]:
        """Distinct alert origins of the given rules (one notice per chat)."""
        seen: set[tuple] = set()
        out: list[dict[str, Any]] = []
        with self._state.lock:
            for rule_id in sorted(rule_ids):
                rt = self._state.rules.get(rule_id)
                if rt is None:
                    continue
                origin = dict(rt.spec.origin)
                route = (origin.get("session_key"), origin.get("platform"), origin.get("chat_id"))
                if route not in seen:
                    seen.add(route)
                    out.append(origin)
        return out

    def finished(self, worker: SourceWorker, source: Any, exc: BaseException) -> str:
        """A finite source (a video file) ran out: stop retrying it and say so once."""
        with suppress(Exception):
            source.close()
        with worker.lock:
            worker.is_open = False
        worker.online = False
        worker.last_error = short_error(exc)
        worker.next_attempt = self._state.clock() + FINISHED_RETRY_S  # never replay a finished file
        return f"Source {worker.key} finished: {worker.last_error}. Watching it has stopped."

    def failed(self, worker: SourceWorker, exc: BaseException, now: float) -> str | None:
        """Record a failure and schedule the retry; returns the offline notice, once."""
        settings = self._state.settings
        worker.failures += 1
        worker.last_error = short_error(exc)
        worker.online = False
        if isinstance(exc, SecretScopeUnavailable):
            # Not an outage: a tool or command call will prime the secret cache. Poll quickly.
            worker.next_attempt = now + SECRET_SCOPE_RETRY_S
            return None
        if worker.offline_since is None:
            worker.offline_since = now
        delay = min(
            settings.backoff_max,
            settings.backoff_initial * (2 ** min(worker.failures - 1, 30)),
        )
        worker.next_attempt = now + delay
        logger.info(
            "sentinel: source %s failed (%s); retry in %.0fs", worker.key, worker.last_error, delay
        )
        if worker.offline_notified or now - worker.offline_since < settings.offline_grace:
            return None
        worker.offline_notified = True
        return (
            f"Source {worker.key} is offline ({worker.last_error}). Retrying automatically "
            f"(at least every {settings.backoff_max:.0f}s); I'll say when it is back."
        )

    def ok(self, worker: SourceWorker) -> str | None:
        """Mark the source healthy; returns the back-online notice if offline was announced."""
        notice = (
            f"Source {worker.key} is back online; watching resumed."
            if worker.offline_notified
            else None
        )
        worker.failures = 0
        worker.offline_since = None
        worker.offline_notified = False
        worker.online = True
        worker.last_error = ""
        return notice
