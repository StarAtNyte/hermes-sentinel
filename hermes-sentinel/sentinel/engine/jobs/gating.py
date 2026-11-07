"""Gate before judge: decide per frame and per rule whether a judge job is worth queueing."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from ...judging import motion_sequence
from ...rules import RuleSpec, RuleState
from ..models import Job, RuleRuntime
from ..state import EngineState
from .queue import JobQueue
from .timing import schedule_active

logger = logging.getLogger(__name__)

__all__ = ["DefaultGateFactory", "FrameGate", "TRIGGER_IDLE_MIN_SECONDS"]

TRIGGER_IDLE_MIN_SECONDS = 6 * 3600.0


class DefaultGateFactory:
    """Builds the production :class:`~sentinel.vision.gate.ChangeGate` for a rule."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (config, clock and motion policy are read per call)."""
        self._state = state

    def __call__(self, spec: RuleSpec) -> Any:
        """A change gate configured for ``spec`` (ROI, idle tick, recent-frame buffer)."""
        from ...vision.gate import ChangeGate, GateConfig  # numpy is only needed once frames flow

        # Idle ticks are for condition/absence rules. A trigger rule needs real changes (plus
        # the start-up look), so its safety-net tick is stretched to TRIGGER_IDLE_MIN_SECONDS.
        idle = self._state.config.idle_check_seconds
        if spec.mode == "trigger" and idle > 0:
            idle = max(idle, TRIGGER_IDLE_MIN_SECONDS)
        return ChangeGate(
            GateConfig(idle_check_seconds=idle, recent_frames=self._recent_frames_for(spec)),
            roi=spec.roi,
            clock=self._state.clock,
        )

    def _recent_frames_for(self, spec: RuleSpec) -> int:
        """Frames a rule's gate keeps: only motion rules read them, so the others keep none."""
        return max(0, self._state.policy.motion_frames) if spec.motion else 0


class FrameGate:
    """Runs each captured frame through every watching rule's gate and queues what changed."""

    def __init__(
        self,
        state: EngineState,
        queue: JobQueue,
        gate_factory: Callable[[RuleSpec], Any],
    ) -> None:
        """``gate_factory(spec)`` builds a rule's gate the first time it sees a frame."""
        self._state = state
        self._queue = queue
        self._gate_factory = gate_factory

    def process_frame(self, rule_ids: list[str], now: float, frame: Any) -> None:
        """Gate ``frame`` for each of ``rule_ids`` and queue the resulting jobs together."""
        staged: list[tuple[RuleRuntime, Job]] = []
        for rule_id in rule_ids:
            with self._state.lock:
                rt = self._state.rules.get(rule_id)
            if (
                rt is None
                or not rt.spec.enabled
                or not schedule_active(rt.spec, self._state.wall())
            ):
                continue
            job = self._gate_one(rt, now, frame)
            if job is not None:
                staged.append((rt, job))
        self._queue.enqueue_all(staged)

    def _gate_one(self, rt: RuleRuntime, now: float, frame: Any) -> Job | None:
        """The job for one rule and frame, or ``None`` (cooling down, quiet, backing off)."""
        with suppress(Exception):
            rt.machine.poll(now)
        if rt.machine.state is RuleState.COOLDOWN:
            # A verdict could not fire (and would be thrown away): do not pay for it.
            return None
        try:
            if rt.gate is None:
                rt.gate = self._gate_factory(rt.spec)
            result = rt.gate.process(frame, now)
        except Exception as exc:  # noqa: BLE001 - one bad frame/rule must not stop others
            logger.warning("sentinel: gate failed for rule %s: %s", rt.spec.id, exc)
            self._state.record_error(exc)
            return None
        return self._job_for(rt, result, now, frame)

    def _job_for(self, rt: RuleRuntime, result: Any, now: float, frame: Any) -> Job | None:
        """The judge job a gate result calls for, or ``None`` (quiet, or backing off)."""
        reason = ""
        if getattr(result, "changed", False):
            reason = "change"
        elif getattr(result, "tick", False):
            reason = "tick"
        elif rt.machine.state is RuleState.CANDIDATE and now >= rt.next_confirm_at:
            reason = "confirm"
        if not reason or now < rt.judge_backoff_until:
            return None
        job = Job(rt.spec.id, now, frame, reason, bbox=getattr(result, "bbox", None))
        job.sequence = self._motion_sequence(rt, frame, now)
        return job

    def _motion_sequence(self, rt: RuleRuntime, frame: Any, now: float) -> tuple[Any, ...]:
        """The ordered frames a motion rule is judged on (empty: judge a single still)."""
        frames = self._state.policy.motion_frames
        if not rt.spec.motion or frames < 2:
            return ()
        recent = getattr(rt.gate, "recent_frames", None)
        if recent is None:
            return ()
        try:
            return motion_sequence(recent(), frame, now, frames)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("sentinel: no recent frames for rule %s: %s", rt.spec.id, exc)
            return ()
