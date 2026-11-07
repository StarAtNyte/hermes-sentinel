"""RouteBook: session routes learned from slash commands, persisted as JSON."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..routing import clean_value, route_from_kwargs
from .json_file import read_json_object, write_json_atomic

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
        data = read_json_object(self._path, "routes")
        for session_id, route in list(data.items())[-self._limit :]:
            if isinstance(session_id, str) and isinstance(route, dict):
                clean = route_from_kwargs(route)
                if clean:
                    self._routes[session_id] = clean

    def _save_locked(self) -> None:
        write_json_atomic(self._path, self._routes, "routes")

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
