"""The bounded judge queue: one job per rule, oldest dropped when full, batches of one frame."""

from __future__ import annotations

import threading
from collections import deque

from ..models import Job, RuleRuntime
from ..state import EngineState

__all__ = ["JobQueue"]


class JobQueue:
    """Holds pending judge jobs and hands them to workers.

    At most one job is queued per rule (a newer frame replaces the queued one) and
    ``settings.queue_size`` jobs overall. A rule is never handed to two workers at once, so
    verdicts reach its state machine in order.
    """

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (rules, settings, batching policy, stop flag)."""
        self._state = state
        # Guards items, dropped and every rule's queued / in_flight fields.
        self.condition = threading.Condition(threading.Lock())
        self.items: deque[Job] = deque()
        self.dropped = 0

    def __len__(self) -> int:
        """Number of queued jobs."""
        with self.condition:
            return len(self.items)

    # ---- producing -------------------------------------------------------------------------

    def enqueue_all(self, staged: list[tuple[RuleRuntime, Job]]) -> None:
        """Queue one frame's jobs together so a worker sees them all and can batch them."""
        if not staged:
            return
        with self.condition:
            for rt, job in staged:
                self._enqueue_locked(rt, job)
            self.condition.notify_all()

    def _enqueue_locked(self, rt: RuleRuntime, job: Job) -> None:
        """Queue ``job`` for ``rt`` (condition held): at most one queued job per rule."""
        queued = rt.queued
        if queued is not None:  # newest frame wins; keep the strongest reason
            queued.ts, queued.frame = job.ts, job.frame
            queued.bbox, queued.sequence = job.bbox, job.sequence
            if job.reason == "change":
                queued.reason = job.reason
            return
        if len(self.items) >= int(self._state.settings.queue_size):
            old = self.items.popleft()
            old_rt = self._state.rules.get(old.rule_id)
            if old_rt is not None and old_rt.queued is old:
                old_rt.queued = None
                old_rt.dropped += 1
            self.dropped += 1
        self.items.append(job)
        rt.queued = job

    # ---- consuming -------------------------------------------------------------------------

    def take_group(self) -> list[Job]:
        """Pop the next job group without waiting (empty when nothing can run now)."""
        with self.condition:
            return self._take_group_locked()

    def wait_for_group(self) -> list[Job]:
        """Block until a group is available; empty once the engine is stopping."""
        stop = self._state.stop
        with self.condition:
            group = self._take_group_locked()
            while not group and not stop.is_set():
                self.condition.wait(0.5)
                group = self._take_group_locked()
        return group

    def release_group(self, group: list[Job]) -> None:
        """Mark every rule of a finished group idle again and wake the workers."""
        with self.condition:
            for job in group:
                rt = self._state.rules.get(job.rule_id)
                if rt is not None:
                    rt.in_flight = False
            self.condition.notify_all()

    def _take_job_locked(self) -> Job | None:
        """Pop the oldest job whose rule is not being judged right now (condition held)."""
        rules = self._state.rules
        orphans = [j for j in self.items if j.rule_id not in rules]
        for job in orphans:
            self.items.remove(job)
        for job in self.items:
            rt = rules.get(job.rule_id)
            if rt is None or rt.in_flight:
                continue
            self.items.remove(job)
            if rt.queued is job:
                rt.queued = None
            rt.in_flight = True
            return job
        return None

    def _take_group_locked(self) -> list[Job]:
        """Pop the next job plus queued peers that can share its model call (condition held).

        Peers are other idle rules on the same source whose job holds the very same frame
        (so they were queued by one capture), up to the policy's batch cap. Motion rules judge
        a sequence rather than the shared still, so they never join or lead a batch.
        """
        first = self._take_job_locked()
        if first is None:
            return []
        group = [first]
        lead = self._state.rules.get(first.rule_id)
        cap = self._state.policy.batch_cap
        if cap <= 1 or lead is None or lead.spec.motion:
            return group
        for job in list(self.items):
            if len(group) >= cap:
                break
            rt = self._state.rules.get(job.rule_id)
            if rt is None or rt.in_flight or rt.spec.motion or job.frame is not first.frame:
                continue
            if rt.source_key != lead.source_key:
                continue
            self.items.remove(job)
            rt.queued = None
            rt.in_flight = True
            group.append(job)
        return group

    # ---- maintenance -----------------------------------------------------------------------

    def remove_rule(self, rule_id: str) -> None:
        """Drop every queued job of ``rule_id``."""
        with self.condition:
            self.items = deque(j for j in self.items if j.rule_id != rule_id)

    def clear(self) -> None:
        """Empty the queue and forget each rule's queued job (engine stop)."""
        with self.condition:
            for job in self.items:
                rt = self._state.rules.get(job.rule_id)
                if rt is not None:
                    rt.queued = None
            self.items.clear()

    def wake_all(self) -> None:
        """Wake every waiting worker (so it can notice the stop flag)."""
        with self.condition:
            self.condition.notify_all()
