"""Judge workers: take queued jobs, judge them through the pipeline, apply the verdicts."""

from __future__ import annotations

import logging

from ...judging import JudgeItem, JudgingPipeline
from ...rules import RuleState
from ..infra import ExampleCache
from ..jobs import JobQueue, schedule_active
from ..models import Job, Prepared
from ..state import EngineState
from .applier import VerdictApplier

logger = logging.getLogger(__name__)

__all__ = ["JudgeRunner"]


class JudgeRunner:
    """Runs judge jobs, either on worker threads (:meth:`loop`) or synchronously.

    A job is skipped when its rule was disabled, is outside its schedule, is cooling down or
    the engine is stopping. Several rules queued from one frame are judged as one group.
    """

    def __init__(
        self,
        state: EngineState,
        queue: JobQueue,
        pipeline: JudgingPipeline,
        applier: VerdictApplier,
        examples: ExampleCache,
    ) -> None:
        """Share ``state`` and the queue; ``pipeline`` judges, ``applier`` records outcomes."""
        self._state = state
        self._queue = queue
        self._pipeline = pipeline
        self._applier = applier
        self._examples = examples

    def loop(self) -> None:
        """Worker thread body: judge groups until the engine stops."""
        while not self._state.stop.is_set():
            group = self._queue.wait_for_group()
            if not group:
                return
            self._run_group_safely(group)

    def run_pending(self, max_jobs: int = 1000) -> int:
        """Run queued judge jobs on the calling thread (synchronous mode). Returns the count."""
        done = 0
        while done < max_jobs:
            group = self._queue.take_group()
            if not group:
                break
            self._run_group_safely(group)
            done += len(group)
        return done

    def _run_group_safely(self, group: list[Job]) -> None:
        try:
            self._run_group(group)
        except Exception as exc:  # noqa: BLE001 - a worker must never die
            logger.exception("sentinel: judge job failed for rules %s", [j.rule_id for j in group])
            self._state.record_error(exc)
        finally:
            self._queue.release_group(group)

    def _prepare(self, job: Job) -> Prepared | None:
        """The judge input for ``job``, or ``None`` when the rule should not be judged now."""
        state = self._state
        with state.lock:
            rt = state.rules.get(job.rule_id)
        if rt is None or not rt.spec.enabled:
            return None
        spec = rt.spec
        if not schedule_active(spec, state.wall()):
            return None
        if rt.machine.cooling_down(state.clock()):
            return None  # queued before the rule fired; the verdict could not count
        latched = rt.machine.state is RuleState.FIRING
        if state.stop.is_set() or not rt.pacing.begin_call(
            state.clock(), confirming=job.confirming, latched=latched
        ):
            return None
        item = JudgeItem(
            spec=spec,
            frame=job.frame,
            examples=tuple(self._examples.load(spec.id)),
            sequence=job.sequence,
            bbox=job.bbox,
        )
        return Prepared(job, rt, item)

    def _run_group(self, group: list[Job]) -> None:
        """Judge the jobs of one group (usually one) and feed each verdict to its rule."""
        prepared = [p for p in (self._prepare(job) for job in group) if p is not None]
        if not prepared:
            return
        outcomes = self._pipeline.run([p.item for p in prepared])
        for entry, outcome in zip(prepared, outcomes, strict=True):
            if self._state.stop.is_set():
                return  # unloaded while the model was thinking: no store writes, no alerts
            if outcome.budget_refused:
                entry.rt.budget_skips += 1
                entry.rt.pacing.cancel_call()
            elif outcome.verdict is not None:
                self._applier.apply(entry.rt, entry.job, outcome.verdict, outcome.from_fallback)
