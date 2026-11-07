"""Background retention prune for processes whose engine is not running."""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["BackgroundPruner"]


class BackgroundPruner:
    """Runs ``store.prune_if_due()`` on a daemon thread, at most one at a time."""

    def __init__(self) -> None:
        """Start idle."""
        # A plain flag, no lock: a race can at worst start a second thread, and
        # prune_if_due only does work when a prune is due.
        self.busy = False

    def kick(self, store: Any, engine: Any) -> None:
        """Prune in the background unless the engine's own scheduler is running or nothing is due.

        Called from tool/command entry points; a cheap no-op in the common case.
        """
        if self.busy or (engine is not None and engine.running):
            return
        due = getattr(store, "prune_due", None)
        if due is not None and not due():
            return
        self.busy = True
        threading.Thread(
            target=self._run, args=(store,), name="sentinel-prune", daemon=True
        ).start()

    def _run(self, store: Any) -> None:
        try:
            store.prune_if_due()
        except Exception as exc:  # noqa: BLE001 - retention is best effort
            logger.debug("sentinel: background prune failed: %s", exc)
        finally:
            self.busy = False
