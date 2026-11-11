"""Turns prepared rule checks into verdicts: batching, fallback and budget accounting."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from ..vision.judge import BatchQuestion, Verdict
from .items import JudgeItem, Outcome
from .policy import JudgingPolicy

__all__ = ["BATCH_USAGE_ID", "JudgingPipeline"]

#: Usage-table id that batched calls are charged to.
BATCH_USAGE_ID = "batch"

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
        reserve: Callable[..., bool],
        stopped: Callable[[], bool],
    ) -> None:
        """``reserve(spec, rule_id=...)`` spends one vision call; ``stopped`` is the stop flag."""
        self._judge = judge
        self._policy = policy
        self._reserve = reserve
        self._stopped = stopped

    def run(self, items: Sequence[JudgeItem]) -> list[Outcome]:
        """One outcome per item, in the same order. Never raises."""
        done: dict[str, Outcome] = {}
        attempted: set[str] = set()
        cap = self._policy.batch_cap
        batchable = [item for item in items if item.batchable]
        if cap > 1 and len(batchable) >= 2:
            for chunk in _chunks(batchable, cap):
                if len(chunk) >= 2:
                    attempted.update(item.spec.id for item in chunk)
                    done.update(self._run_batch(chunk))
        return [self._outcome_for(item, done, attempted) for item in items]

    def _outcome_for(
        self, item: JudgeItem, done: dict[str, Outcome], attempted: set[str]
    ) -> Outcome:
        """The batch's outcome for ``item``, else its own call (flagged when a batch missed it)."""
        outcome = done.get(item.spec.id)
        if outcome is not None:
            return outcome
        single = self.single(item)
        if item.spec.id not in attempted:
            return single
        return replace(single, from_fallback=True)

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
        # Charged to a shared pseudo-rule so no single rule's usage is inflated; the exhausted
        # notice still goes to the first rule's origin, which is where the frame came from.
        if not self._reserve(chunk[0].spec, rule_id=BATCH_USAGE_ID):
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
