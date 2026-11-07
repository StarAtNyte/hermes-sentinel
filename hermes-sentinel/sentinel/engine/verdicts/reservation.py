"""Reserving vision budget before a model call."""

from __future__ import annotations

import logging
import sqlite3

from ...rules import RuleSpec
from ...storage import StoreError
from ..state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["BudgetReserver"]


class BudgetReserver:
    """Callable that spends one vision call; the budget's single exhaustion notice is posted."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (budget and alerter)."""
        self._state = state

    def __call__(self, spec: RuleSpec, rule_id: str | None = None) -> bool:
        """Consume one vision call (charged to ``rule_id`` or the rule); False when refused."""
        state = self._state
        try:
            allowed = state.budget.try_consume(rule_id=rule_id or spec.id)
        except Exception as exc:  # noqa: BLE001 - a store error must not crash judging
            logger.warning("sentinel: budget check failed: %s", exc)
            return False
        if not allowed:
            notice = self._exhausted_notice()
            if notice:
                state.alerter.notice(notice, origin=spec.origin)
        return allowed

    def _exhausted_notice(self) -> str | None:
        try:
            return self._state.budget.take_exhausted_notice()
        except (StoreError, OSError, sqlite3.Error) as exc:
            logger.debug("sentinel: budget notice unavailable: %s", exc)
            return None
