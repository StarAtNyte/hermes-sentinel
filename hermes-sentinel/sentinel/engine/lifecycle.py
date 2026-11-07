"""Engine start, standby and stop: the threads, the process lock and the shutdown flag."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .capture import CaptureWorkers
from .infra import ProcessLock
from .jobs import JobQueue
from .models import SourceWorker
from .state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["Lifecycle"]


@dataclass
class _Stopping:
    """Thread handles and workers detached from the state while ``stop()`` holds the lock."""

    was_running: bool
    workers: list[SourceWorker]
    threads: list[threading.Thread | None]


class Lifecycle:
    """Owns the judge, scheduler and standby threads and the one-engine-per-profile lock.

    ``stop()`` sets the shutdown flag under ``state.lock``; after that no ``start()`` can
    succeed, so a tool call in flight at unload cannot revive the engine.
    """

    def __init__(
        self,
        state: EngineState,
        capture: CaptureWorkers,
        queue: JobQueue,
        process_lock: ProcessLock | None,
        *,
        judge_loop: Callable[[], None],
        scheduler_loop: Callable[[], None],
        sync_rules: Callable[[], None],
    ) -> None:
        """``judge_loop`` / ``scheduler_loop`` are thread bodies; ``sync_rules`` loads the store."""
        self._state = state
        self._capture = capture
        self._queue = queue
        self._process_lock = process_lock
        self._judge_loop = judge_loop
        self._scheduler_loop = scheduler_loop
        self._sync_rules = sync_rules
        # These handles are read and written only under state.lock.
        self._judge_threads: list[threading.Thread] = []
        self._scheduler: threading.Thread | None = None
        self._standby: threading.Thread | None = None

    def start(self, *, allow_standby: bool = True) -> bool:
        """Load rules from the store and start the threads. Idempotent.

        Returns ``False`` (and starts a standby retry thread when ``allow_standby``) if another
        process owns the engine for this data directory, and always ``False`` once :meth:`stop`
        has run.
        """
        state = self._state
        with state.lock:
            if state.shutdown:
                return False
            if state.running:
                return True
            if self._process_lock is not None and not self._process_lock.acquire():
                state.last_error = "another Hermes process is already running Sentinel"
                if allow_standby:
                    self._start_standby_locked()
                return False
            self._mark_running_locked()
            self._sync_rules()
            if state.threaded:
                self._start_threads_locked()
            logger.info("sentinel: engine started (%d rules)", len(state.rules))
            return True

    def _mark_running_locked(self) -> None:
        state = self._state
        state.stop.clear()
        state.standing_by = False
        state.running = True
        state.started_at = state.wall()
        for rt in state.rules.values():  # a job cut off by a timed-out stop()
            rt.in_flight = False

    def _start_threads_locked(self) -> None:
        for i in range(int(self._state.settings.workers)):
            thread = threading.Thread(
                target=self._judge_loop, name=f"sentinel-judge-{i}", daemon=True
            )
            self._judge_threads.append(thread)
            thread.start()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, name="sentinel-scheduler", daemon=True
        )
        self._scheduler.start()
        self._capture.start_all_locked()

    def _start_standby_locked(self) -> None:
        state = self._state
        if state.shutdown:
            return
        state.standing_by = True
        if not state.threaded or (self._standby is not None and self._standby.is_alive()):
            return
        self._standby = threading.Thread(
            target=self._standby_loop, name="sentinel-standby", daemon=True
        )
        self._standby.start()

    def _standby_loop(self) -> None:
        state = self._state
        while not state.stop.wait(state.settings.sync_interval):
            # Take over in place: never spawns another standby thread.
            if self.start(allow_standby=False):
                return

    def stop(self, timeout: float | None = None) -> None:
        """Stop and join every thread, close sources, release the engine lock. Idempotent."""
        timeout = self._state.settings.stop_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, timeout)
        stopping = self._detach_for_stop()
        self._queue.wake_all()
        self._capture.signal_stop(stopping.workers)
        self._join_all([*stopping.threads, *(w.thread for w in stopping.workers)], deadline)
        self._capture.release(stopping.workers)
        self._queue.clear()
        if self._process_lock is not None:
            self._process_lock.release()
        if stopping.was_running:
            logger.info("sentinel: engine stopped")

    def _detach_for_stop(self) -> _Stopping:
        """Set the shutdown flags and take the thread handles, atomically."""
        state = self._state
        with state.lock:
            state.shutdown = True
            state.stop.set()
            was_running = state.running
            state.running = False
            state.standing_by = False
            workers = list(state.workers.values())
            threads: list[threading.Thread | None] = list(self._judge_threads)
            self._judge_threads.clear()
            scheduler, self._scheduler = self._scheduler, None
            standby, self._standby = self._standby, None
        threads += [scheduler, standby]
        return _Stopping(was_running, workers, threads)

    @staticmethod
    def _join_all(threads: list[threading.Thread | None], deadline: float) -> None:
        current = threading.current_thread()
        for thread in threads:
            if thread is not None and thread is not current and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
