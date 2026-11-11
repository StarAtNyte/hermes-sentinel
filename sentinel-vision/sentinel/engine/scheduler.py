"""The once-a-second scheduler: state-machine polls, retention prune and rule re-sync."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["Scheduler"]


class Scheduler:
    """Periodic housekeeping, run by the scheduler thread or called directly by tests."""

    def __init__(self, state: EngineState, sync_rules: Callable[[], None]) -> None:
        """``sync_rules()`` re-reads the store (so rules made by another process appear)."""
        self._state = state
        self._sync_rules = sync_rules
        self._next_sync = 0.0

    def maintenance(self) -> None:
        """Poll state machines, prune old data if due, and re-sync rules every sync interval."""
        state = self._state
        now = state.clock()
        with state.lock:
            runtimes = list(state.rules.values())
        for rt in runtimes:
            try:
                rt.machine.poll(now)
            except Exception as exc:  # noqa: BLE001 - one rule's poll must not stop the others
                logger.debug("sentinel: poll failed for %s: %s", rt.spec.id, exc)
        try:
            state.store.prune_if_due()
        except Exception as exc:  # noqa: BLE001 - retention is best effort
            logger.warning("sentinel: retention prune failed: %s", exc)
        if now >= self._next_sync:
            self._next_sync = now + state.settings.sync_interval
            self._sync_rules()

    def loop(self) -> None:
        """Scheduler thread body: run :meth:`maintenance` until the engine stops."""
        state = self._state
        self._next_sync = state.clock() + state.settings.sync_interval
        while not state.stop.wait(state.settings.scheduler_interval):
            try:
                self.maintenance()
            except Exception as exc:  # noqa: BLE001 - the scheduler must never die
                logger.exception("sentinel: maintenance failed")
                state.record_error(exc)
