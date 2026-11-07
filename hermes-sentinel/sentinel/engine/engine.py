"""The :class:`Engine` facade over the collaborators (see the :mod:`sentinel.engine` docstring)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..alerts import Alerter
from ..budget import Budget
from ..config import SentinelConfig
from ..judging import HourlyCap, JudgingPipeline, SecondOpinion
from ..rules import RuleSpec
from ..vision import Judge
from .capture import CaptureWorkers, FrameGrabber, SourceHealth
from .infra import ExampleCache, ProcessLock
from .jobs import DefaultGateFactory, FrameGate, JobQueue
from .lifecycle import Lifecycle
from .rulebook import RuleBook
from .scheduler import Scheduler
from .settings import EngineSettings
from .state import EngineState
from .status import StatusReporter
from .verdicts import (
    BudgetReserver,
    EventRecorder,
    JudgeHealth,
    JudgeRunner,
    RuleTester,
    VerdictApplier,
)

__all__ = ["Engine"]


class Engine:
    """Owns all background work. Thread-safe; nothing here blocks the agent loop."""

    def __init__(
        self,
        *,
        store: Any,
        judge: Judge,
        alerter: Alerter,
        budget: Budget,
        config: SentinelConfig,
        settings: EngineSettings | None = None,
        source_factory: Callable[[str], Any] | None = None,
        source_key: Callable[[str], str] | None = None,
        gate_factory: Callable[[RuleSpec], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        lock_path: Path | str | None = None,
        threaded: bool = True,
    ) -> None:
        """Build the engine; nothing runs until :meth:`start` (or :meth:`add_rule`)."""
        self.state = EngineState(
            store=store,
            judge=judge,
            alerter=alerter,
            budget=budget,
            config=config,
            settings=settings,
            source_factory=source_factory,
            source_key=source_key,
            clock=clock,
            wall_clock=wall_clock,
            threaded=threaded,
        )
        self.queue = JobQueue(self.state)
        self.examples = ExampleCache(store)
        self._wire_capture(gate_factory)
        self._wire_judging(judge, clock)
        self._wire_lifecycle(lock_path)
        self._status = StatusReporter(self.state, self.queue)

    def _wire_capture(self, gate_factory: Callable[[RuleSpec], Any] | None) -> None:
        """Build the capture side: gating, source workers and the rule registry."""
        self.default_gate = DefaultGateFactory(self.state)
        gating = FrameGate(self.state, self.queue, gate_factory or self.default_gate)
        self.capture = CaptureWorkers(self.state, SourceHealth(self.state), gating.process_frame)
        self.rulebook = RuleBook(self.state, self.capture, self.queue)

    def _wire_lifecycle(self, lock_path: Path | str | None) -> None:
        """Build the scheduler and the start/stop lifecycle over the other collaborators."""
        self.scheduler = Scheduler(self.state, self.rulebook.sync_from_store)
        self.lifecycle = Lifecycle(
            self.state,
            self.capture,
            self.queue,
            ProcessLock(lock_path) if lock_path is not None else None,
            judge_loop=self.runner.loop,
            scheduler_loop=self.scheduler.loop,
            sync_rules=self.rulebook.sync_from_store,
        )

    def _wire_judging(self, judge: Judge, clock: Callable[[], float]) -> None:
        """Build the judge-side collaborators (pipeline, second opinion, runner, tester)."""
        state = self.state
        reserve = BudgetReserver(state)
        stopped = state.stop.is_set
        self.pipeline = JudgingPipeline(judge, state.policy, reserve, stopped)
        self.second_opinion = SecondOpinion(
            judge,
            state.policy,
            HourlyCap(state.policy.second_opinion_per_hour, clock=clock),
            reserve,
            stopped,
        )
        self.judge_health = JudgeHealth(state)
        applier = VerdictApplier(
            state, self.second_opinion, self.judge_health, EventRecorder(state)
        )
        self.runner = JudgeRunner(state, self.queue, self.pipeline, applier, self.examples)
        self.grabber = FrameGrabber(state)
        self.tester = RuleTester(state, self.grabber, self.examples, reserve)

    # ---- services and state -----------------------------------------------------------

    @property
    def store(self) -> Any:
        """The event/rule store."""
        return self.state.store

    @property
    def config(self) -> SentinelConfig:
        """The plugin configuration the engine was built with."""
        return self.state.config

    @property
    def last_error(self) -> str:
        """Most recent background error (shortened, credentials masked); empty if none."""
        return self.state.last_error

    @property
    def started_at(self) -> float | None:
        """Wall clock of the last successful start, or ``None``."""
        return self.state.started_at

    @property
    def running(self) -> bool:
        """True while the engine's threads (or synchronous mode) are active."""
        return self.state.running

    @property
    def standing_by(self) -> bool:
        """True when another process holds the engine lock and this one is waiting."""
        return self.state.standing_by

    # ---- lifecycle --------------------------------------------------------------------

    def start(self) -> bool:
        """Load rules from the store and start the threads. Idempotent.

        Returns ``False`` (and starts a standby retry thread) when another process owns the
        engine for this data directory, and always ``False`` once :meth:`stop` has been called
        (an engine is never restarted after shutdown).
        """
        return self.lifecycle.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop and join every thread, close sources, release the engine lock. Idempotent."""
        self.lifecycle.stop(timeout)

    # ---- rules ------------------------------------------------------------------------

    def add_rule(self, spec: RuleSpec) -> None:
        """Install (or replace) a rule and start the engine if it is not running."""
        self.rulebook.install(spec)
        if not self.state.running:
            self.start()

    def update_rule(self, spec: RuleSpec) -> None:
        """Apply an edited spec (alias of :meth:`add_rule` without the lazy start)."""
        self.rulebook.install(spec)

    def remove_rule(self, rule_id: str) -> bool:
        """Forget a rule; False when it was unknown."""
        return self.rulebook.remove(rule_id)

    def sync_from_store(self) -> None:
        """Make the in-memory rules match the store (add, update, remove). Never raises."""
        self.rulebook.sync_from_store()

    def rule_ids(self) -> list[str]:
        """Sorted ids of every installed rule."""
        return self.rulebook.rule_ids()

    # ---- sources ----------------------------------------------------------------------

    def step_source(self, key: str) -> str:
        """One capture iteration for the source ``key`` (synchronous; used by tests).

        Returns ``frame``, ``offline``, ``waiting``, ``not_ready`` or ``missing``.
        """
        return self.capture.step_source(key)

    def source_key(self, spec: str) -> str:
        """The grouping key (credentials redacted) a rule's source spec maps to."""
        return self.state.source_key(spec)

    def source_keys(self) -> list[str]:
        """Sorted keys of the sources currently watched."""
        return self.capture.source_keys()

    def source_infos(self) -> dict[str, dict[str, Any]]:
        """Per capture worker: online flag, last error, seconds since the last frame, rule ids."""
        return self.capture.source_infos()

    def latest_frame(self, key: str) -> Any | None:
        """The capture thread's newest frame for source ``key``; never opens a device."""
        return self.capture.latest_frame(key)

    def grab_frame(self, spec: str) -> Any:
        """A current frame of ``spec`` (latest capture if fresh, else a one-off read).

        Raises :class:`~sentinel.sources.SourceError` with a friendly message.
        """
        return self.grabber.grab(spec)

    # ---- judging ----------------------------------------------------------------------

    def run_pending_judgements(self, max_jobs: int = 1000) -> int:
        """Run queued judge jobs on the calling thread (synchronous mode). Returns the count."""
        return self.runner.run_pending(max_jobs)

    def queue_length(self) -> int:
        """Number of queued judge jobs."""
        return len(self.queue)

    def test_rule(self, rule_id: str) -> dict[str, Any]:
        """Judge the rule once on a fresh frame without feeding its state machine."""
        return self.tester.test(rule_id)

    def maintenance(self) -> None:
        """Periodic housekeeping (the scheduler thread calls this once a second)."""
        self.scheduler.maintenance()

    # ---- status -----------------------------------------------------------------------

    def rule_status(self, rule_id: str) -> dict[str, Any] | None:
        """Live status of one rule, or ``None`` if it is unknown."""
        return self._status.rule_status(rule_id)

    def status(self) -> dict[str, Any]:
        """Engine-wide status: flags, sources, queue depth, counters and the last error."""
        return self._status.status()
