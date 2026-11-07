"""Turns prepared rule checks into verdicts: batching, fallback and budget accounting."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from ..rules import RuleSpec
from ..vision.judge import BatchQuestion, Verdict
from .items import JudgeItem, Outcome
from .policy import JudgingPolicy

__all__ = ["JudgingPipeline"]

logger = logging.getLogger(__name__)


def _chunks(items: Sequence[JudgeItem], size: int) -> list[list[JudgeItem]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


class JudgingPipeline:
    """Judges a group of rule checks, paying the vision budget once per model call.

    Checks on the same frame that need nothing special are answered together (at most
    ``policy.batch_cap`` per call); everything else, and anything a batch could not answer,
    is judged on its own. Stateless, so any number of workers can share one.
    """

    def __init__(
        self,
        judge: Any,
        policy: JudgingPolicy,
        reserve: Callable[[RuleSpec], bool],
        stopped: Callable[[], bool],
    ) -> None:
        """``reserve`` consumes one vision call for a rule; ``stopped`` is the shutdown flag."""
        self._judge = judge
        self._policy = policy
        self._reserve = reserve
        self._stopped = stopped

    def run(self, items: Sequence[JudgeItem]) -> list[Outcome]:
        """One outcome per item, in the same order. Never raises."""
        done: dict[str, Outcome] = {}
        cap = self._policy.batch_cap
        batchable = [item for item in items if item.batchable]
        if cap > 1 and len(batchable) >= 2:
            for chunk in _chunks(batchable, cap):
                if len(chunk) >= 2:
                    done.update(self._run_batch(chunk))
        return [done[item.spec.id] if item.spec.id in done else self.single(item) for item in items]

    def single(self, item: JudgeItem) -> Outcome:
        """Judge one item with its own call (budget reserved first)."""
        if self._stopped():
            return Outcome(item)
        if not self._reserve(item.spec):
            return Outcome(item, budget_refused=True)
        extra = {"sequence": item.sequence} if len(item.sequence) >= 2 else {}
        verdict = self._judge.judge(
            item.frame,
            item.spec.question,
            rule_id=item.spec.id,
            roi=item.spec.roi,
            examples=list(item.examples),
            **extra,
        )
        return Outcome(item, verdict)

    def _run_batch(self, chunk: list[JudgeItem]) -> dict[str, Outcome]:
        """One call for the whole chunk; unanswered rules fall back to their own calls."""
        if self._stopped():
            return {item.spec.id: Outcome(item) for item in chunk}
        if not self._reserve(chunk[0].spec):
            return {item.spec.id: Outcome(item, budget_refused=True) for item in chunk}
        questions = [BatchQuestion(i.spec.id, i.spec.question, i.spec.roi) for i in chunk]
        reply = self._judge.judge_batch(chunk[0].frame, questions)
        if reply.call_failed:  # the model is unreachable: retrying one by one would only repeat it
            failure = Verdict(None, error=reply.error or "batch call failed", elapsed=reply.elapsed)
            return {item.spec.id: Outcome(item, failure) for item in chunk}
        done: dict[str, Outcome] = {}
        for item in chunk:
            verdict = reply.verdicts.get(item.spec.id)
            if verdict is not None:
                done[item.spec.id] = Outcome(item, verdict)
        if reply.missing:
            logger.info(
                "sentinel: batch left %d rule(s) unanswered; judging them singly",
                len(reply.missing),
            )
        return done
