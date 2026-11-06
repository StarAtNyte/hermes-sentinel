"""RouteBook: session routes learned from slash commands, persisted as JSON."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .routing import clean_value, route_from_kwargs

logger = logging.getLogger(__name__)


class RouteBook:
    """Bounded, thread-safe map ``session_id -> route`` learned from slash commands.

    Persisted as JSON at ``path`` (when given) so a route survives restarts. Never raises.
    """

    def __init__(self, path: Path | str | None = None, *, limit: int = 256) -> None:
        """Create the book; ``path`` enables persistence, ``limit`` bounds the entries."""
        self._path = Path(path) if path is not None else None
        self._limit = max(1, int(limit))
        # Guards _routes and the routes file write.
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
        session_id = clean_value((kwargs or {}).get("session_id")) or route.get("session_key")
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
        """The remembered route for ``session_id`` (empty if unknown)."""
        if not session_id:
            return {}
        with self._lock:
            return dict(self._routes.get(session_id, {}))
