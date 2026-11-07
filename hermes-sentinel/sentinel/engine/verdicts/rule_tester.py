"""Judging a rule once on demand, without touching its state machine."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from ...rules import RuleSpec
from ...sources import SourceError
from ...storage import StoreError
from ..capture import FrameGrabber
from ..infra import ExampleCache, short_error
from ..state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["RuleTester"]


class RuleTester:
    """Backs the "test this rule" tool and dashboard action."""

    def __init__(
        self,
        state: EngineState,
        grabber: FrameGrabber,
        examples: ExampleCache,
        reserve: Callable[[RuleSpec], bool],
    ) -> None:
        """``reserve(spec)`` spends one vision call; ``grabber`` supplies a fresh frame."""
        self._state = state
        self._grabber = grabber
        self._examples = examples
        self._reserve = reserve

    def test(self, rule_id: str) -> dict[str, Any]:
        """Judge the rule once on a fresh frame without feeding its state machine."""
        state = self._state
        spec = self._find_spec(rule_id)
        if spec is None:
            return {"ok": False, "error": f"no rule with id {rule_id!r}"}
        try:
            frame = self._grabber.grab(spec.source)
        except SourceError as exc:
            return {"ok": False, "error": f"could not read {spec.source}: {short_error(exc)}"}
        if not self._reserve(spec):
            return {"ok": False, "error": state.budget.status().describe()}
        verdict = state.judge.judge(
            frame,
            spec.question,
            rule_id=spec.id,
            roi=spec.roi,
            examples=self._examples.load(spec.id),
        )
        out = {"ok": verdict.ok, "rule_id": spec.id, "question": spec.question, **verdict.to_dict()}
        if verdict.ok:
            evidence = (not verdict.match) if spec.mode == "absence" else verdict.match
            out["would_count"] = bool(evidence and verdict.confidence >= spec.confidence_threshold)
        return out

    def _find_spec(self, rule_id: str) -> RuleSpec | None:
        """The loaded rule's spec, else the stored one, else ``None``."""
        with self._state.lock:
            rt = self._state.rules.get(rule_id)
            spec = rt.spec if rt is not None else None
        if spec is not None:
            return spec
        try:
            return self._state.store.get_rule(rule_id)
        except (StoreError, OSError, sqlite3.Error) as exc:
            logger.debug("sentinel: could not read rule %s: %s", rule_id, exc)
            return None
