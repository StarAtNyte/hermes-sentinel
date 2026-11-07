"""Capture threads: one per source, shared by every rule watching it."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, NamedTuple

from ...sources import SourceExhausted, SourceNotReady
from ..infra import short_error
from ..models import SourceWorker
from ..state import EngineState
from .health import SourceHealth

logger = logging.getLogger(__name__)

__all__ = ["CaptureWorkers"]

FrameSink = Callable[[list[str], float, Any], None]


class _Step(NamedTuple):
    """Result of one open or read attempt."""

    outcome: str
    notice: str | None = None
    payload: Any = None  # the source after opening, or the frame after reading


class CaptureWorkers:
    """Starts, steps and stops the per-source capture threads.

    Each frame goes to ``on_frame(rule_ids, now, frame)`` (the gating stage). A source that
    fails is closed and reopened with exponential backoff; :class:`SourceHealth` decides which
    notices go out.
    """

    def __init__(self, state: EngineState, health: SourceHealth, on_frame: FrameSink) -> None:
        """Share ``state``; ``on_frame`` receives every captured frame for gating."""
        self._state = state
        self._health = health
        self._on_frame = on_frame

    # ---- attach / detach (callers hold state.lock) -----------------------------------------

    def attach_locked(self, rule_id: str, key: str, spec: str) -> None:
        """Register ``rule_id`` on the worker for ``key`` (created if new); start it if running."""
        state = self._state
        worker = state.workers.get(key)
        if worker is None:
            worker = SourceWorker(key=key, spec=spec)
            state.workers[key] = worker
        worker.rule_ids.add(rule_id)
        if state.running:
            self.start_worker_locked(worker)

    def detach_locked(self, rule_id: str, key: str) -> None:
        """Remove ``rule_id`` from its worker; a worker nobody uses any more is stopped."""
        state = self._state
        worker = state.workers.get(key)
        if worker is None:
            return
        worker.rule_ids.discard(rule_id)
        if worker.rule_ids:
            return
        del state.workers[key]
        worker.stop.set()
        thread = worker.thread
        if thread is None or not thread.is_alive():
            self.close_source(worker)
        # else: the capture thread closes its source on the way out (never join under state.lock).

    def start_all_locked(self) -> None:
        """Start a thread for every known worker (engine start)."""
        for worker in list(self._state.workers.values()):
            self.start_worker_locked(worker)

    def start_worker_locked(self, worker: SourceWorker) -> None:
        """Start the capture thread of ``worker`` unless threads are off or it already runs."""
        if not self._state.threaded or (worker.thread is not None and worker.thread.is_alive()):
            return
        worker.stop.clear()
        # Run in a copy of the starting thread's context so contextvars (e.g. Hermes' secret
        # scope under multiplexing) set by the tool call that created/resumed the rule are
        # visible to open(); rules started at boot or by the scheduler have none.
        ctx = contextvars.copy_context()
        worker.thread = threading.Thread(
            target=ctx.run,
            args=(self._capture_loop, worker),
            name=f"sentinel-capture-{worker.key}"[:60],
            daemon=True,
        )
        worker.thread.start()

    # ---- stopping --------------------------------------------------------------------------

    def signal_stop(self, workers: list[SourceWorker]) -> None:
        """Ask every capture loop in ``workers`` to end."""
        for worker in workers:
            worker.stop.set()

    def release(self, workers: list[SourceWorker]) -> None:
        """Close the sources of stopped ``workers`` (the threads were joined by the caller)."""
        current = threading.current_thread()
        for worker in workers:
            thread = worker.thread
            worker.thread = None
            if thread is not None and thread is not current and thread.is_alive():
                # Stuck in open()/read(): close from a helper so stop() never blocks on it.
                threading.Thread(
                    target=self.close_source, args=(worker,), name="sentinel-close", daemon=True
                ).start()
            else:
                self.close_source(worker)

    def close_source(self, worker: SourceWorker) -> None:
        """Close the worker's source. close() may block, so it runs outside the worker lock."""
        with worker.lock:
            source, worker.is_open = worker.source, False
        if source is not None:
            try:
                source.close()
            except Exception as exc:  # noqa: BLE001 - a failing close must not stop shutdown
                logger.debug("sentinel: closing %s failed: %s", worker.key, exc)

    # ---- the capture loop ------------------------------------------------------------------

    def _capture_loop(self, worker: SourceWorker) -> None:
        state = self._state
        interval = state.settings.capture_interval
        try:
            while not worker.stop.is_set() and not state.stop.is_set():
                started = time.monotonic()
                outcome = self._guarded_step(worker)
                if outcome in ("offline", "waiting"):
                    wait = max(0.05, min(1.0, worker.next_attempt - state.clock()))
                else:
                    wait = max(0.0, interval - (time.monotonic() - started))
                worker.stop.wait(wait)
        finally:
            self.close_source(worker)

    def _guarded_step(self, worker: SourceWorker) -> str:
        """One step that survives any exception (the loop must never die)."""
        try:
            return self.step_worker(worker)
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            logger.exception("sentinel: capture step failed for %s", worker.key)
            self._state.record_error(exc)
            return "error"

    def step_source(self, key: str) -> str:
        """One capture iteration for the source ``key`` (synchronous; used by tests).

        Returns ``frame``, ``offline``, ``waiting``, ``not_ready`` or ``missing``.
        """
        with self._state.lock:
            worker = self._state.workers.get(key)
        if worker is None:
            return "missing"
        return self.step_worker(worker)

    def step_worker(self, worker: SourceWorker) -> str:
        """Open if needed, read one frame, publish notices and hand the frame to gating."""
        now = self._state.clock()
        with worker.lock:
            rule_ids = sorted(worker.rule_ids)
            is_open, source = worker.is_open, worker.source
        # open()/read() can block for seconds: never hold worker.lock across them (the source
        # serialises itself), so stop() and grab_frame() are never stuck behind a reconnect.
        opened = _Step("frame", payload=source)
        if not is_open:
            opened = self._open(worker, source, now)
            if opened.outcome == "waiting":
                return "waiting"
        read = _Step(opened.outcome)
        if opened.outcome == "frame":
            read = self._read(worker, opened.payload, now)
            if read.outcome == "not_ready":
                return "not_ready"
        # Notices and gating run outside any worker lock.
        notice = read.notice if opened.outcome == "frame" else opened.notice
        if notice:
            self._health.notify(rule_ids, notice)
        if read.outcome == "frame":
            self._on_frame(rule_ids, now, read.payload)
        return read.outcome

    def _open(self, worker: SourceWorker, source: Any, now: float) -> _Step:
        """Open the worker's source; ``waiting`` before the next retry, ``offline`` on failure."""
        if now < worker.next_attempt:
            return _Step("waiting")
        try:
            if source is None:
                source = self._state.source_factory(worker.spec)
                worker.source = source
            source.open()
            with worker.lock:
                worker.is_open = True
        except Exception as exc:  # noqa: BLE001 - every source failure is handled
            return _Step("offline", self._health.failed(worker, exc, now))
        return _Step("frame", payload=source)

    def _read(self, worker: SourceWorker, source: Any, now: float) -> _Step:
        """Read one frame; map end-of-file and read errors to ``finished`` / ``offline``."""
        try:
            _ts, frame = source.read()
        except SourceNotReady:
            return _Step("not_ready")
        except SourceExhausted as exc:
            return _Step("finished", self._health.finished(worker, source, exc))
        except Exception as exc:  # noqa: BLE001 - a broken read reconnects with backoff
            with suppress(Exception):
                source.close()
            with worker.lock:
                worker.is_open = False
            return _Step("offline", self._health.failed(worker, exc, now))
        notice = self._health.ok(worker)
        with worker.latest_lock:
            worker.latest = (now, frame)
        worker.frames += 1
        return _Step("frame", notice, frame)

    # ---- read-only views -------------------------------------------------------------------

    def source_infos(self) -> dict[str, dict[str, Any]]:
        """Per capture worker: online flag, last error, seconds since the last frame, rule ids.

        Pure in-memory read (no device I/O).
        """
        now = self._state.clock()
        with self._state.lock:
            workers = list(self._state.workers.values())
        out: dict[str, dict[str, Any]] = {}
        for w in workers:
            with w.latest_lock:
                latest = w.latest
            out[w.key] = {
                "online": bool(w.online),
                "error": short_error(w.last_error) if w.last_error else None,
                "last_frame_age_s": round(max(0.0, now - latest[0]), 1) if latest else None,
                "rules": sorted(w.rule_ids),
            }
        return out

    def latest_frame(self, key: str) -> Any | None:
        """The capture thread's newest frame for source ``key``; never opens a device."""
        with self._state.lock:
            worker = self._state.workers.get(key)
        if worker is None:
            return None
        with worker.latest_lock:
            latest = worker.latest
        return latest[1] if latest else None

    def source_keys(self) -> list[str]:
        """Sorted keys of the sources currently watched."""
        with self._state.lock:
            return sorted(self._state.workers)
