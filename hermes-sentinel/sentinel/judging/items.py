"""Value types passed between the engine and the judging pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..rules import RuleSpec
from ..vision.judge import Example, SequenceFrame, Verdict

__all__ = ["JudgeItem", "Outcome"]


@dataclass(frozen=True)
class JudgeItem:
    """One rule check ready to be judged: the frame, teaching examples and optional context."""

    spec: RuleSpec
    frame: Any
    examples: tuple[Example, ...] = ()
    sequence: tuple[SequenceFrame, ...] = ()
    bbox: tuple[int, int, int, int] | None = None

    @property
    def batchable(self) -> bool:
        """True when the check needs nothing a shared call cannot give (no examples, no motion)."""
        return not self.examples and len(self.sequence) < 2


@dataclass(frozen=True)
class Outcome:
    """What happened to one item: a verdict, a budget refusal, or neither (stopped)."""

    item: JudgeItem
    verdict: Verdict | None = None
    budget_refused: bool = False
