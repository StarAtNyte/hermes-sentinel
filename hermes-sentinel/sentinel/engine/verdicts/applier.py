"""Applying one verdict to its rule: second opinion, bookkeeping, state machine, firing."""

from __future__ import annotations

from ...judging import SecondOpinion
from ...rules import RuleState
from ...vision import Verdict
from ..jobs import confirm_spacing
from ..models import Job, RuleRuntime
from ..state import EngineState
from .judge_health import JudgeHealth
from .recorder import EventRecorder

__all__ = ["VerdictApplier"]


class VerdictApplier:
    """Turns a judged verdict into rule state changes, judge-health updates and alerts."""

    def __init__(
        self,
        state: EngineState,
        second_opinion: SecondOpinion,
        judge_health: JudgeHealth,
        recorder: EventRecorder,
    ) -> None:
        """Share the engine ``state`` and the collaborators that handle each outcome."""
        self._state = state
        self._second_opinion = second_opinion
        self._judge_health = judge_health
        self._recorder = recorder

    def apply(
        self, rt: RuleRuntime, job: Job, verdict: Verdict, from_fallback: bool = False
    ) -> None:
        """Run the optional second opinion, record the verdict and advance the rule."""
        state = self._state
        spec = rt.spec
        if not from_fallback:
            verdict = self._second_opinion.review(
                spec, rt.machine.state, job.frame, job.bbox, verdict
            )
        if state.stop.is_set():
            return
        now = state.clock()
        rt.checks += 1
        rt.last_verdict = verdict
        rt.last_judged_at = state.wall()
        rt.last_reason = job.reason
        if not verdict.ok:
            self._judge_health.failed(rt, verdict, now)
            return
        rt.consecutive_errors = 0
        rt.judge_backoff_until = 0.0
        self._judge_health.succeeded(rt.source_key, now)
        step = rt.machine.observe(verdict.match, verdict.confidence, now)
        if step.state is RuleState.CANDIDATE:
            rt.next_confirm_at = now + confirm_spacing(spec, state.settings.confirm_interval)
        if step.fired is not None:
            self._recorder.fire(rt, job, verdict)
