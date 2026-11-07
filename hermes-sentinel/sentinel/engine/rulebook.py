"""The engine's rule registry: installing, updating, removing and syncing rules."""

from __future__ import annotations

import logging

from ..rules import RuleSpec, RuleStateMachine
from .capture import CaptureWorkers
from .jobs import JobQueue
from .jobs.pacing import RulePacing
from .models import RuleRuntime
from .state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["RuleBook"]


class RuleBook:
    """Keeps ``state.rules`` in step with the store and wires rules to capture workers."""

    def __init__(self, state: EngineState, capture: CaptureWorkers, queue: JobQueue) -> None:
        """Share ``state``; rules attach to ``capture`` workers and leave ``queue`` when removed."""
        self._state = state
        self._capture = capture
        self._queue = queue

    def install(self, spec: RuleSpec) -> None:
        """Install (or replace) a rule's runtime and attach or detach its source worker."""
        state = self._state
        with state.lock:
            rt = state.rules.get(spec.id)
            if rt is None:
                rt = self._new_runtime(spec)
                state.rules[spec.id] = rt
            else:
                self._update_locked(rt, spec)
            if spec.enabled:
                self._capture.attach_locked(spec.id, rt.source_key, spec.source)
            else:
                self._capture.detach_locked(spec.id, rt.source_key)

    def remove(self, rule_id: str) -> bool:
        """Forget a rule and its queued jobs; False when it was unknown."""
        with self._state.lock:
            rt = self._state.rules.pop(rule_id, None)
            if rt is None:
                return False
            self._capture.detach_locked(rule_id, rt.source_key)
        self._queue.remove_rule(rule_id)
        return True

    def sync_from_store(self) -> None:
        """Make the in-memory rules match the store (add, update, remove). Never raises."""
        state = self._state
        try:
            specs = {s.id: s for s in state.store.list_rules()}
        except Exception as exc:  # noqa: BLE001 - a broken store keeps the rules we have
            logger.warning("sentinel: could not load rules: %s", exc)
            return
        with state.lock:
            for rule_id in [r for r in state.rules if r not in specs]:
                self.remove(rule_id)
            for spec in specs.values():
                rt = state.rules.get(spec.id)
                if rt is None or _changed(rt.spec, spec):
                    self.install(spec)

    def rule_ids(self) -> list[str]:
        """Sorted ids of every installed rule."""
        with self._state.lock:
            return sorted(self._state.rules)

    def _new_runtime(self, spec: RuleSpec) -> RuleRuntime:
        state = self._state
        return RuleRuntime(
            spec=spec,
            machine=RuleStateMachine(
                spec, clock=state.clock, stale_after_seconds=state.config.stale_after_seconds
            ),
            source_key=state.source_key(spec.source),
            pacing=RulePacing(
                state.config.rule_min_interval_seconds,
                state.config.rule_calls_per_hour,
                clock=state.clock,
                latched_min_interval=state.config.latched_min_interval_seconds,
            ),
        )

    def _update_locked(self, rt: RuleRuntime, spec: RuleSpec) -> None:
        """Apply an edited spec to an existing runtime (``state.lock`` held)."""
        old = rt.spec
        rt.machine.update_spec(spec)
        rt.spec = spec
        if old.roi != spec.roi and rt.gate is not None:
            self._move_roi(rt, spec)
        new_key = self._state.source_key(spec.source)
        if new_key != rt.source_key:
            self._capture.detach_locked(spec.id, rt.source_key)
            rt.source_key = new_key
            rt.gate = None
            rt.machine.reset()
        if old.motion != spec.motion:
            rt.gate = None  # rebuilt on the next frame with the right frame buffer
        if old.enabled and not spec.enabled:
            rt.machine.reset()

    @staticmethod
    def _move_roi(rt: RuleRuntime, spec: RuleSpec) -> None:
        try:
            rt.gate.set_roi(spec.roi)
        except Exception:  # noqa: BLE001 - a gate that cannot move its ROI is rebuilt
            rt.gate = None


def _changed(old: RuleSpec, new: RuleSpec) -> bool:
    """True when ``new`` differs from the installed ``old`` spec."""
    return old.updated_at != new.updated_at or old.to_dict() != new.to_dict()
