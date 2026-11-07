"""The process-wide pointer to the loaded Sentinel, for the separately imported dashboard API."""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["active_sentinel", "clear_active_sentinel", "set_active_sentinel"]

_ACTIVE: Any = None
_ACTIVE_LOCK = threading.Lock()  # guards _ACTIVE


def set_active_sentinel(sentinel: Any) -> None:
    """Publish the loaded Sentinel for the dashboard API; ``None`` clears it.

    The dashboard is a separately imported module and reaches the same store and engine the
    tools use through this pointer.
    """
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = sentinel


def active_sentinel() -> Any:
    """The published Sentinel, or ``None`` when none is loaded."""
    with _ACTIVE_LOCK:
        return _ACTIVE


def clear_active_sentinel(sentinel: Any) -> None:
    """Unpublish ``sentinel`` if (and only if) it is the one currently published."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is sentinel:
            _ACTIVE = None
