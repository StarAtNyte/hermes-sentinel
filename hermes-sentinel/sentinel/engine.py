"""The watch engine: capture threads, change gates, judge workers, scheduler and health.

Data flow per source::

    capture thread --frame--> ChangeGate (one per rule, ROI aware)
        --changed / tick / confirm--> bounded judge queue (drop oldest)
            --> judge worker (pool of 2): budget -> Judge -> RuleStateMachine
                --fired--> snapshot + event in the store -> Alerter

* **One capture thread per source**, shared by every rule watching it. A source that fails is
  closed and reopened with exponential backoff (1 s doubling to 60 s). Once it has been down
  for ``offline_grace`` seconds one "offline" notice goes out; one "back online" notice follows
  when it recovers.
* **Gate before judge**: a frame is judged only when the rule's gate reports a change, the
  gate's periodic idle tick is due, or the rule is collecting k-of-n confirmation verdicts
  (``candidate`` state; spaced ``max(confirm_interval, hold / (n - 1))`` apart).
* **Cost guards**: rules in ``cooldown`` are not queued (their verdicts could not fire),
  and the default gate ticks ``trigger`` rules at most every 6 h (condition/absence rules use
  ``idle_check_seconds``).
* **Bounded queue**: at most one queued job per rule (a newer frame replaces the queued one)
  and ``queue_size`` jobs overall; when full the oldest job is dropped. A rule is never
  judged by two workers at once, so verdicts reach its state machine in order.
* **Batching**: when several rules on one source were queued from the very same frame, one
  worker takes up to ``batch_max_rules`` of them and judges them in a single multi-question
  call (:class:`judging.JudgingPipeline`); rules with teaching examples or a motion question
  are judged alone, and rules the batch did not answer fall back to their own calls. Per-rule
  state machines, cooldowns and events are untouched.
* **Motion**: a rule with ``motion`` set is judged on its gate's recent frames in order
  (one call). **Second opinion**: a borderline positive verdict that would start or confirm an
  alert is re-checked once on a crop of the changed region (:class:`judging.SecondOpinion`).
* **Budget**: every vision call (single, batch, fallback, second opinion) is reserved
  through :class:`budget.Budget`. When refused the job is skipped and the budget's single
  exhaustion notice is posted.
* **Judge failures** (no network, refused task, bad reply) back the rule off (5 s doubling to
  300 s) and post one notice per failure episode; the gate keeps running.
* **Scheduler** (once a second): polls state machines (cooldown end, staleness), runs the
  store's hourly retention prune and re-syncs rules from the store every ``sync_interval``
  (so rules created by another Hermes process are picked up).
* **One engine per data directory**: an exclusive lock file stops two Hermes processes on the
  same profile from opening the same camera twice. A process that loses keeps retrying in a
  standby thread and takes over when the owner exits.

* **Secret scope**: a capture thread runs in a copy of the context of the call that started
  it, and every tool or command call primes an in-memory secret cache
  (:meth:`runtime.Sentinel.prime_secrets`). A camera whose ``${SECRET}`` cannot be read on a
  scope-less thread (boot or scheduler, multiplexing gateway) reports "waiting for secret
  scope" and re-checks every few seconds instead of going offline; single-profile Hermes
  reads secrets on any thread and never waits.
* **Finite sources**: a video file that reaches its end stops the worker with one notice
  instead of reconnecting and replaying it.

Every thread is a daemon and :meth:`Engine.stop` joins them all. ``threaded=False`` builds an
engine that never starts threads; tests drive it with :meth:`step_source`,
:meth:`run_pending_judgements` and :meth:`maintenance`, deterministically.
"""

from __future__ import annotations

import contextvars
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .alerts import Alerter
from .budget import Budget
from .config import SentinelConfig
from .judging import (
    HourlyCap,
    JudgeItem,
    JudgingPipeline,
    JudgingPolicy,
    SecondOpinion,
    motion_sequence,
)
from .rules import RuleSpec, RuleState, RuleStateMachine
from .sources import (
    SecretScopeUnavailable,
    SourceError,
    SourceExhausted,
    SourceNotReady,
    SourceSpec,
    open_source,
    redact_url,
)
from .vision import Example, Judge, Verdict, encode_jpeg

logger = logging.getLogger(__name__)

__all__ = ["Engine", "EngineSettings", "ProcessLock", "default_source_key"]

MAX_ERROR_TEXT = 240
TRIGGER_IDLE_MIN_SECONDS = 6 * 3600.0
EXAMPLE_CACHE_SIZE = 32
EXAMPLE_CACHE_BYTES = 16 * 1024 * 1024  # examples are small now; cap the cache by bytes too
MAX_EXAMPLE_FILE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class EngineSettings:
    """Internal timing knobs (not user config; tests shorten them)."""

    capture_interval: float = 0.5
    confirm_interval: float = 2.0
    queue_size: int = 8
    workers: int = 2
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    offline_grace: float = 5.0
    judge_backoff_initial: float = 5.0
    judge_backoff_max: float = 300.0
    scheduler_interval: float = 1.0
    sync_interval: float = 30.0
    stop_timeout: float = 5.0
    max_frame_age: float = 5.0
    judge_error_rearm: float = 600.0

    def __post_init__(self) -> None:
        for name in (
            "capture_interval",
            "confirm_interval",
            "backoff_initial",
            "backoff_max",
            "judge_backoff_initial",
            "judge_backoff_max",
            "scheduler_interval",
            "sync_interval",
            "stop_timeout",
            "max_frame_age",
            "judge_error_rearm",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if self.offline_grace < 0:
            raise ValueError("offline_grace must be >= 0")
        if not 1 <= int(self.queue_size) <= 1024:
            raise ValueError("queue_size must be 1..1024")
        if not 1 <= int(self.workers) <= 16:
            raise ValueError("workers must be 1..16")


def default_source_key(spec: str, named_streams: dict[str, str] | None = None) -> str:
    """Grouping key for a rule's source: the canonical spec, or the raw text if unparsable."""
    try:
        return SourceSpec.parse(spec, named_streams=named_streams).canonical()
    except Exception:  # noqa: BLE001
        return str(spec).strip()


FINISHED_RETRY_S = 10 * 365 * 86400.0  # effectively never (finite, so status stays JSON-safe)
SECRET_SCOPE_RETRY_S = 5.0  # how often a camera waiting for the secret scope re-checks


def _short_error(exc: BaseException | str) -> str:
    text = str(exc) if not isinstance(exc, str) else exc
    text = " ".join(text.split()) or (type(exc).__name__ if not isinstance(exc, str) else "")
    # Belt and braces: never echo a URL with credentials (sources already redact).
    words = [redact_url(w) if "://" in w else w for w in text.split(" ")]
    text = " ".join(words)
    return text[:MAX_ERROR_TEXT]


# --------------------------------------------------------------------------- process lock


class ProcessLock:
    """Exclusive, non-blocking lock on a file (``fcntl``/``msvcrt``); released on close.

    Where neither is available the lock always succeeds (single-process assumption).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh: Any = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        with self._lock:
            if self._fh is not None:
                return True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(self.path, "a+b")  # noqa: SIM115 - kept open while held
            except OSError as exc:
                logger.warning("sentinel: cannot open lock file %s: %s", self.path, exc)
                return False
            try:
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:  # pragma: no cover - Windows
                    try:
                        import msvcrt

                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    except ImportError:
                        pass
            except OSError:
                fh.close()
                return False
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()).encode())
                fh.flush()
            except OSError:
                pass
            self._fh = fh
            return True

    def release(self) -> None:
        with self._lock:
            fh, self._fh = self._fh, None
        if fh is not None:
            with suppress(OSError):
                fh.close()  # closing drops flock / msvcrt locks


# --------------------------------------------------------------------------- runtime state


@dataclass(eq=False)
class _Job:
    rule_id: str
    ts: float
    frame: Any
    reason: str
    bbox: tuple[int, int, int, int] | None = None  # gate's changed region, input-frame pixels
    sequence: tuple[Any, ...] = ()  # motion rules: earlier frames plus this one, oldest first


@dataclass(eq=False)
class _Prepared:
    """A dequeued job that passed the pre-judge checks, with its rule and judge input."""

    job: _Job
    rt: _RuleRuntime
    item: JudgeItem


@dataclass(eq=False)
class _RuleRuntime:
    spec: RuleSpec
    machine: RuleStateMachine
    source_key: str
    gate: Any = None
    queued: _Job | None = None
    in_flight: bool = False
    checks: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    budget_skips: int = 0
    dropped: int = 0
    last_verdict: Verdict | None = None
    last_judged_at: float | None = None  # wall clock
    last_reason: str = ""
    next_confirm_at: float = 0.0
    judge_backoff_until: float = 0.0
    last_event_id: int | None = None


@dataclass(eq=False)
class _SourceWorker:
    key: str
    spec: str
    rule_ids: set[str] = field(default_factory=set)
    source: Any = None
    is_open: bool = False
    online: bool | None = None  # None: never connected yet
    failures: int = 0
    next_attempt: float = 0.0
    offline_since: float | None = None
    offline_notified: bool = False
    last_error: str = ""
    latest: tuple[float, Any] | None = None
    frames: int = 0
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)  # state swaps only, no I/O
    latest_lock: threading.Lock = field(default_factory=threading.Lock)


# --------------------------------------------------------------------------- engine


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
        self.store = store
        self.judge = judge
        self.alerter = alerter
        self.budget = budget
        self.config = config
        self.settings = settings or EngineSettings()
        self._source_factory = source_factory or (
            lambda spec: open_source(spec, named_streams=self.config.stream_urls)
        )
        self._source_key = source_key or (
            lambda spec: default_source_key(spec, self.config.stream_urls)
        )
        self._gate_factory = gate_factory or self._default_gate
        self._clock = clock
        self._wall = wall_clock
        self._threaded = bool(threaded)
        self._process_lock = ProcessLock(lock_path) if lock_path is not None else None

        self._lock = threading.RLock()
        self._rules: dict[str, _RuleRuntime] = {}
        self._workers: dict[str, _SourceWorker] = {}
        self._qcond = threading.Condition(threading.Lock())
        self._queue: deque[_Job] = deque()
        self._dropped = 0
        self._judge_threads: list[threading.Thread] = []
        self._scheduler: threading.Thread | None = None
        self._standby: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._standing_by = False
        self._next_sync = 0.0
        self._shutdown = False
        # per source key: [notice sent, time of first success since the last failure]
        self._judge_errors: dict[str, list[Any]] = {}
        self._fired = 0
        self._examples: OrderedDict[tuple[str, float], bytes] = OrderedDict()
        self._examples_lock = threading.Lock()
        self._policy = JudgingPolicy.from_config(config)
        stopped = self._stop.is_set
        self._pipeline = JudgingPipeline(judge, self._policy, self._reserve_budget, stopped)
        self._second_opinion = SecondOpinion(
            judge,
            self._policy,
            HourlyCap(self._policy.second_opinion_per_hour, clock=clock),
            self._reserve_budget,
            stopped,
        )
        self.last_error = ""
        self.started_at: float | None = None  # wall clock of the last successful start

    # ---- lifecycle --------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def standing_by(self) -> bool:
        """True when another process holds the engine lock and this one is waiting."""
        return self._standing_by

    def start(self) -> bool:
        """Load rules from the store and start the threads. Idempotent.

        Returns ``False`` (and starts a standby retry thread) when another process owns the
        engine for this data directory, and always ``False`` once :meth:`stop` has been called
        (an engine is never restarted after shutdown).
        """
        return self._start(allow_standby=True)

    def _start(self, *, allow_standby: bool) -> bool:
        with self._lock:
            if self._shutdown:
                return False
            if self._running:
                return True
            if self._process_lock is not None and not self._process_lock.acquire():
                self.last_error = "another Hermes process is already running Sentinel"
                if allow_standby:
                    self._start_standby_locked()
                return False
            self._stop.clear()
            self._standing_by = False
            self._running = True
            self.started_at = self._wall()
            for rt in self._rules.values():  # a job cut off by a timed-out stop()
                rt.in_flight = False
            self.sync_from_store()
            if self._threaded:
                for i in range(int(self.settings.workers)):
                    t = threading.Thread(
                        target=self._judge_loop, name=f"sentinel-judge-{i}", daemon=True
                    )
                    self._judge_threads.append(t)
                    t.start()
                self._scheduler = threading.Thread(
                    target=self._scheduler_loop, name="sentinel-scheduler", daemon=True
                )
                self._scheduler.start()
                for worker in list(self._workers.values()):
                    self._start_worker_locked(worker)
            logger.info("sentinel: engine started (%d rules)", len(self._rules))
            return True

    def _start_standby_locked(self) -> None:
        if self._shutdown:
            return
        self._standing_by = True
        if not self._threaded or (self._standby is not None and self._standby.is_alive()):
            return

        def standby() -> None:
            while not self._stop.wait(self.settings.sync_interval):
                # Take over in place: never spawns another standby thread.
                if self._start(allow_standby=False):
                    return

        self._standby = threading.Thread(target=standby, name="sentinel-standby", daemon=True)
        self._standby.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop and join every thread, close sources, release the engine lock. Idempotent."""
        timeout = self.settings.stop_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._shutdown = True
            self._stop.set()
            was_running = self._running
            self._running = False
            self._standing_by = False
            workers = list(self._workers.values())
            threads = list(self._judge_threads)
            self._judge_threads.clear()
            scheduler, self._scheduler = self._scheduler, None
            standby, self._standby = self._standby, None
        with self._qcond:
            self._qcond.notify_all()
        for worker in workers:
            worker.stop.set()
        current = threading.current_thread()
        for t in [*threads, scheduler, standby, *(w.thread for w in workers)]:
            if t is not None and t is not current and t.is_alive():
                t.join(max(0.0, deadline - time.monotonic()))
        for worker in workers:
            thread = worker.thread
            worker.thread = None
            if thread is not None and thread is not current and thread.is_alive():
                # Stuck in open()/read(): close from a helper so stop() never blocks on it.
                threading.Thread(
                    target=self._close_worker_source,
                    args=(worker,),
                    name="sentinel-close",
                    daemon=True,
                ).start()
            else:
                self._close_worker_source(worker)
        with self._qcond:
            for job in self._queue:
                rt = self._rules.get(job.rule_id)
                if rt is not None:
                    rt.queued = None
            self._queue.clear()
        if self._process_lock is not None:
            self._process_lock.release()
        if was_running:
            logger.info("sentinel: engine stopped")

    # ---- rules ------------------------------------------------------------------------

    def _default_gate(self, spec: RuleSpec) -> Any:
        from .vision.gate import ChangeGate, GateConfig  # numpy is only needed once frames flow

        # Idle ticks are for condition/absence rules. A trigger rule needs real changes (plus
        # the start-up look), so its safety-net tick is stretched to TRIGGER_IDLE_MIN_SECONDS.
        idle = self.config.idle_check_seconds
        if spec.mode == "trigger" and idle > 0:
            idle = max(idle, TRIGGER_IDLE_MIN_SECONDS)
        return ChangeGate(
            GateConfig(idle_check_seconds=idle),
            roi=spec.roi,
            clock=self._clock,
        )

    def _new_runtime(self, spec: RuleSpec) -> _RuleRuntime:
        return _RuleRuntime(
            spec=spec,
            machine=RuleStateMachine(
                spec, clock=self._clock, stale_after_seconds=self.config.stale_after_seconds
            ),
            source_key=self._source_key(spec.source),
        )

    def add_rule(self, spec: RuleSpec) -> None:
        """Install (or replace) a rule and start the engine if it is not running."""
        self._install(spec)
        if not self._running:
            self.start()

    def update_rule(self, spec: RuleSpec) -> None:
        """Apply an edited spec (alias of :meth:`add_rule` without the lazy start)."""
        self._install(spec)

    def _install(self, spec: RuleSpec) -> None:
        with self._lock:
            rt = self._rules.get(spec.id)
            if rt is None:
                rt = self._new_runtime(spec)
                self._rules[spec.id] = rt
            else:
                old = rt.spec
                rt.machine.update_spec(spec)
                rt.spec = spec
                if old.roi != spec.roi and rt.gate is not None:
                    try:
                        rt.gate.set_roi(spec.roi)
                    except Exception:  # noqa: BLE001
                        rt.gate = None
                new_key = self._source_key(spec.source)
                if new_key != rt.source_key:
                    self._detach_locked(spec.id, rt.source_key)
                    rt.source_key = new_key
                    rt.gate = None
                    rt.machine.reset()
                if old.enabled and not spec.enabled:
                    rt.machine.reset()
            if spec.enabled:
                self._attach_locked(spec.id, rt.source_key, spec.source)
            else:
                self._detach_locked(spec.id, rt.source_key)

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            rt = self._rules.pop(rule_id, None)
            if rt is None:
                return False
            self._detach_locked(rule_id, rt.source_key)
        with self._qcond:
            self._queue = deque(j for j in self._queue if j.rule_id != rule_id)
        return True

    def sync_from_store(self) -> None:
        """Make the in-memory rules match the store (add, update, remove). Never raises."""
        try:
            specs = {s.id: s for s in self.store.list_rules()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentinel: could not load rules: %s", exc)
            return
        with self._lock:
            for rule_id in [r for r in self._rules if r not in specs]:
                self.remove_rule(rule_id)
            for spec in specs.values():
                rt = self._rules.get(spec.id)
                if (
                    rt is None
                    or rt.spec.updated_at != spec.updated_at
                    or (rt.spec.to_dict() != spec.to_dict())
                ):
                    self._install(spec)

    def rule_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._rules)

    # ---- workers ----------------------------------------------------------------------

    def _attach_locked(self, rule_id: str, key: str, spec: str) -> None:
        worker = self._workers.get(key)
        if worker is None:
            worker = _SourceWorker(key=key, spec=spec)
            self._workers[key] = worker
        worker.rule_ids.add(rule_id)
        if self._running:
            self._start_worker_locked(worker)

    def _detach_locked(self, rule_id: str, key: str) -> None:
        worker = self._workers.get(key)
        if worker is None:
            return
        worker.rule_ids.discard(rule_id)
        if worker.rule_ids:
            return
        del self._workers[key]
        worker.stop.set()
        thread = worker.thread
        if thread is None or not thread.is_alive():
            self._close_worker_source(worker)
        # else: the capture thread closes its source on the way out (never join under _lock).

    def _start_worker_locked(self, worker: _SourceWorker) -> None:
        if not self._threaded or (worker.thread is not None and worker.thread.is_alive()):
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

    def _close_worker_source(self, worker: _SourceWorker) -> None:
        with worker.lock:  # swap state only; close() may block, so it runs outside the lock
            source, worker.is_open = worker.source, False
        if source is not None:
            try:
                source.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("sentinel: closing %s failed: %s", worker.key, exc)

    def _capture_loop(self, worker: _SourceWorker) -> None:
        interval = self.settings.capture_interval
        try:
            while not worker.stop.is_set() and not self._stop.is_set():
                started = time.monotonic()
                try:
                    outcome = self._step_worker(worker)
                except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                    logger.exception("sentinel: capture step failed for %s", worker.key)
                    self.last_error = _short_error(exc)
                    outcome = "error"
                if outcome in ("offline", "waiting"):
                    wait = max(0.05, min(1.0, worker.next_attempt - self._clock()))
                else:
                    wait = max(0.0, interval - (time.monotonic() - started))
                worker.stop.wait(wait)
        finally:
            self._close_worker_source(worker)

    def step_source(self, key: str) -> str:
        """One capture iteration for the source ``key`` (synchronous; used by tests).

        Returns ``frame``, ``offline``, ``waiting``, ``not_ready`` or ``missing``.
        """
        with self._lock:
            worker = self._workers.get(key)
        if worker is None:
            return "missing"
        return self._step_worker(worker)

    def source_key(self, spec: str) -> str:
        """The grouping key (credentials redacted) a rule's source spec maps to."""
        return self._source_key(spec)

    def source_infos(self) -> dict[str, dict[str, Any]]:
        """Per capture worker: online flag, last error, seconds since the last frame, rule ids.

        Pure in-memory read (no device I/O)."""
        now = self._clock()
        with self._lock:
            workers = list(self._workers.values())
        out: dict[str, dict[str, Any]] = {}
        for w in workers:
            with w.latest_lock:
                latest = w.latest
            out[w.key] = {
                "online": bool(w.online),
                "error": _short_error(w.last_error) if w.last_error else None,
                "last_frame_age_s": round(max(0.0, now - latest[0]), 1) if latest else None,
                "rules": sorted(w.rule_ids),
            }
        return out

    def latest_frame(self, key: str) -> Any | None:
        """The capture thread's newest frame for source ``key``; never opens a device."""
        with self._lock:
            worker = self._workers.get(key)
        if worker is None:
            return None
        with worker.latest_lock:
            latest = worker.latest
        return latest[1] if latest else None

    def source_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._workers)

    def _step_worker(self, worker: _SourceWorker) -> str:
        now = self._clock()
        notice: str | None = None
        outcome = "frame"
        frame: Any = None
        with worker.lock:
            rule_ids = sorted(worker.rule_ids)
            is_open, source = worker.is_open, worker.source
        # open()/read() can block for seconds: never hold worker.lock across them (the source
        # serialises itself), so stop() and grab_frame() are never stuck behind a reconnect.
        if not is_open:
            if now < worker.next_attempt:
                return "waiting"
            try:
                if source is None:
                    source = self._source_factory(worker.spec)
                    worker.source = source
                source.open()
                with worker.lock:
                    worker.is_open = True
            except Exception as exc:  # noqa: BLE001 - every source failure is handled
                notice = self._source_failed(worker, exc, now)
                outcome = "offline"
        if outcome == "frame":
            try:
                _ts, frame = source.read()
            except SourceNotReady:
                return "not_ready"
            except SourceExhausted as exc:
                notice = self._source_finished(worker, source, exc)
                outcome = "finished"
            except Exception as exc:  # noqa: BLE001
                with suppress(Exception):
                    source.close()
                with worker.lock:
                    worker.is_open = False
                notice = self._source_failed(worker, exc, now)
                outcome = "offline"
            else:
                notice = self._source_ok(worker)
                with worker.latest_lock:
                    worker.latest = (now, frame)
                worker.frames += 1
        # Notices and gating run outside any worker lock.
        if notice:
            for origin in self._origins_for(rule_ids):
                self.alerter.notice(notice, origin=origin)
        if outcome == "frame":
            self._process_frame(rule_ids, now, frame)
        return outcome

    def _origins_for(self, rule_ids: list[str]) -> list[dict[str, Any]]:
        """Distinct alert origins of the given rules (one notice per chat)."""
        seen: set[tuple] = set()
        out: list[dict[str, Any]] = []
        with self._lock:
            for rule_id in sorted(rule_ids):
                rt = self._rules.get(rule_id)
                if rt is None:
                    continue
                origin = dict(rt.spec.origin)
                route = (origin.get("session_key"), origin.get("platform"), origin.get("chat_id"))
                if route not in seen:
                    seen.add(route)
                    out.append(origin)
        return out

    def _source_finished(self, worker: _SourceWorker, source: Any, exc: BaseException) -> str:
        """A finite source (a video file) ran out: stop retrying it and say so once."""
        with suppress(Exception):
            source.close()
        with worker.lock:
            worker.is_open = False
        worker.online = False
        worker.last_error = _short_error(exc)
        worker.next_attempt = self._clock() + FINISHED_RETRY_S  # do not replay a finished file
        return f"Source {worker.key} finished: {worker.last_error}. Watching it has stopped."

    def _source_failed(self, worker: _SourceWorker, exc: BaseException, now: float) -> str | None:
        """Record a failure and schedule the retry; returns the offline notice, once."""
        worker.failures += 1
        worker.last_error = _short_error(exc)
        worker.online = False
        if isinstance(exc, SecretScopeUnavailable):
            # Not an outage: a tool or command call will prime the secret cache. Poll quickly.
            worker.next_attempt = now + SECRET_SCOPE_RETRY_S
            return None
        if worker.offline_since is None:
            worker.offline_since = now
        delay = min(
            self.settings.backoff_max,
            self.settings.backoff_initial * (2 ** min(worker.failures - 1, 30)),
        )
        worker.next_attempt = now + delay
        logger.info(
            "sentinel: source %s failed (%s); retry in %.0fs", worker.key, worker.last_error, delay
        )
        if worker.offline_notified or now - worker.offline_since < self.settings.offline_grace:
            return None
        worker.offline_notified = True
        return (
            f"Source {worker.key} is offline ({worker.last_error}). Retrying automatically "
            f"(at least every {self.settings.backoff_max:.0f}s); I'll say when it is back."
        )

    def _source_ok(self, worker: _SourceWorker) -> str | None:
        """Mark the source healthy; returns the back-online notice if offline was announced."""
        notice = (
            f"Source {worker.key} is back online; watching resumed."
            if worker.offline_notified
            else None
        )
        worker.failures = 0
        worker.offline_since = None
        worker.offline_notified = False
        worker.online = True
        worker.last_error = ""
        return notice

    # ---- gating -----------------------------------------------------------------------

    def _schedule_active(self, spec: RuleSpec) -> bool:
        if spec.schedule is None:
            return True
        try:
            return spec.schedule.is_active(datetime.fromtimestamp(self._wall()))
        except Exception:  # noqa: BLE001
            return True

    def _confirm_spacing(self, spec: RuleSpec) -> float:
        hold = spec.effective_hold_seconds
        steps = max(1, spec.confirm_n - 1)
        return max(self.settings.confirm_interval, hold / steps if hold > 0 else 0.0)

    def _process_frame(self, rule_ids: list[str], now: float, frame: Any) -> None:
        staged: list[tuple[_RuleRuntime, _Job]] = []
        for rule_id in rule_ids:
            with self._lock:
                rt = self._rules.get(rule_id)
            if rt is None or not rt.spec.enabled or not self._schedule_active(rt.spec):
                continue
            with suppress(Exception):
                rt.machine.poll(now)
            if rt.machine.state is RuleState.COOLDOWN:
                # A verdict could not fire (and would be thrown away): do not pay for it.
                continue
            try:
                if rt.gate is None:
                    rt.gate = self._gate_factory(rt.spec)
                result = rt.gate.process(frame, now)
            except Exception as exc:  # noqa: BLE001 - one bad frame/rule must not stop others
                logger.warning("sentinel: gate failed for rule %s: %s", rule_id, exc)
                self.last_error = _short_error(exc)
                continue
            job = self._job_for(rt, result, now, frame)
            if job is not None:
                staged.append((rt, job))
        self._enqueue_all(staged)

    def _job_for(self, rt: _RuleRuntime, result: Any, now: float, frame: Any) -> _Job | None:
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
        job = _Job(rt.spec.id, now, frame, reason, bbox=getattr(result, "bbox", None))
        job.sequence = self._motion_sequence(rt, frame, now)
        return job

    def _motion_sequence(self, rt: _RuleRuntime, frame: Any, now: float) -> tuple[Any, ...]:
        """The ordered frames a motion rule is judged on (empty: judge a single still)."""
        if not rt.spec.motion or self._policy.motion_frames < 2:
            return ()
        recent = getattr(rt.gate, "recent_frames", None)
        if recent is None:
            return ()
        try:
            return motion_sequence(recent(), frame, now, self._policy.motion_frames)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("sentinel: no recent frames for rule %s: %s", rt.spec.id, exc)
            return ()

    def _enqueue_all(self, staged: list[tuple[_RuleRuntime, _Job]]) -> None:
        """Queue one frame's jobs together so a worker sees them all and can batch them."""
        if not staged:
            return
        with self._qcond:
            for rt, job in staged:
                self._enqueue_locked(rt, job)
            self._qcond.notify_all()

    def _enqueue_locked(self, rt: _RuleRuntime, job: _Job) -> None:
        """Queue ``job`` for ``rt`` (``_qcond`` held): at most one queued job per rule."""
        queued = rt.queued
        if queued is not None:  # newest frame wins; keep the strongest reason
            queued.ts, queued.frame = job.ts, job.frame
            queued.bbox, queued.sequence = job.bbox, job.sequence
            if job.reason == "change":
                queued.reason = job.reason
            return
        if len(self._queue) >= int(self.settings.queue_size):
            old = self._queue.popleft()
            old_rt = self._rules.get(old.rule_id)
            if old_rt is not None and old_rt.queued is old:
                old_rt.queued = None
                old_rt.dropped += 1
            self._dropped += 1
        self._queue.append(job)
        rt.queued = job

    # ---- judging ----------------------------------------------------------------------

    def _take_job_locked(self) -> _Job | None:
        """Pop the oldest job whose rule is not being judged right now (``_qcond`` held)."""
        orphans = [j for j in self._queue if j.rule_id not in self._rules]
        for job in orphans:
            self._queue.remove(job)
        for job in self._queue:
            rt = self._rules.get(job.rule_id)
            if rt is None or rt.in_flight:
                continue
            self._queue.remove(job)
            if rt.queued is job:
                rt.queued = None
            rt.in_flight = True
            return job
        return None

    def _take_group_locked(self) -> list[_Job]:
        """Pop the next job plus queued peers that can share its model call (``_qcond`` held).

        Peers are other idle rules on the same source whose job holds the very same frame
        (so they were queued by one capture), up to the policy's batch cap. Motion rules judge
        a sequence rather than the shared still, so they never join or lead a batch.
        """
        first = self._take_job_locked()
        if first is None:
            return []
        group = [first]
        lead = self._rules.get(first.rule_id)
        cap = self._policy.batch_cap
        if cap <= 1 or lead is None or lead.spec.motion:
            return group
        for job in list(self._queue):
            if len(group) >= cap:
                break
            rt = self._rules.get(job.rule_id)
            if rt is None or rt.in_flight or rt.spec.motion or job.frame is not first.frame:
                continue
            if rt.source_key != lead.source_key:
                continue
            self._queue.remove(job)
            rt.queued = None
            rt.in_flight = True
            group.append(job)
        return group

    def _judge_loop(self) -> None:
        while not self._stop.is_set():
            with self._qcond:
                group = self._take_group_locked()
                while not group and not self._stop.is_set():
                    self._qcond.wait(0.5)
                    group = self._take_group_locked()
            if not group:
                return
            self._run_group_safely(group)

    def _run_group_safely(self, group: list[_Job]) -> None:
        try:
            self._run_group(group)
        except Exception as exc:  # noqa: BLE001 - a worker must never die
            logger.exception("sentinel: judge job failed for rules %s", [j.rule_id for j in group])
            self.last_error = _short_error(exc)
        finally:
            self._release_group(group)

    def _release_group(self, group: list[_Job]) -> None:
        """Mark every rule of a finished group idle again and wake the workers."""
        with self._qcond:
            for job in group:
                rt = self._rules.get(job.rule_id)
                if rt is not None:
                    rt.in_flight = False
            self._qcond.notify_all()

    def run_pending_judgements(self, max_jobs: int = 1000) -> int:
        """Run queued judge jobs on the calling thread (synchronous mode). Returns the count."""
        done = 0
        while done < max_jobs:
            with self._qcond:
                group = self._take_group_locked()
            if not group:
                break
            self._run_group_safely(group)
            done += len(group)
        return done

    def queue_length(self) -> int:
        with self._qcond:
            return len(self._queue)

    def _load_examples(self, rule_id: str) -> list[Example]:
        out: list[Example] = []
        try:
            records = [
                *self.store.list_examples(rule_id, "pos", limit=2),
                *self.store.list_examples(rule_id, "neg", limit=2),
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("sentinel: examples unavailable for %s: %s", rule_id, exc)
            return out
        for rec in records:
            data = self._example_bytes(rec.path)
            if data:
                out.append(Example(kind=rec.kind, data=data, note=rec.note or ""))
        return out

    def _example_bytes(self, path: str) -> bytes | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_size <= 0 or st.st_size > MAX_EXAMPLE_FILE_BYTES:
            return None
        key = (path, st.st_mtime)
        with self._examples_lock:
            data = self._examples.get(key)
            if data is not None:
                self._examples.move_to_end(key)
                return data
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        with self._examples_lock:
            self._examples[key] = data
            total = sum(len(v) for v in self._examples.values())
            while self._examples and (
                len(self._examples) > EXAMPLE_CACHE_SIZE or total > EXAMPLE_CACHE_BYTES
            ):
                _, dropped = self._examples.popitem(last=False)
                total -= len(dropped)
        return data

    def _reserve_budget(self, spec: RuleSpec) -> bool:
        try:
            allowed = self.budget.try_consume(rule_id=spec.id)
        except Exception as exc:  # noqa: BLE001 - a store error must not crash judging
            logger.warning("sentinel: budget check failed: %s", exc)
            return False
        if not allowed:
            notice = None
            try:
                notice = self.budget.take_exhausted_notice()
            except Exception:  # noqa: BLE001
                notice = None
            if notice:
                self.alerter.notice(notice, origin=spec.origin)
        return allowed

    def _prepare(self, job: _Job) -> _Prepared | None:
        """The judge input for ``job``, or ``None`` when the rule should not be judged now."""
        with self._lock:
            rt = self._rules.get(job.rule_id)
        if rt is None or not rt.spec.enabled:
            return None
        spec = rt.spec
        if not self._schedule_active(spec):
            return None
        if rt.machine.state is RuleState.COOLDOWN:
            return None  # queued before the rule fired; the verdict could not count
        if self._stop.is_set():
            return None
        item = JudgeItem(
            spec=spec,
            frame=job.frame,
            examples=tuple(self._load_examples(spec.id)),
            sequence=job.sequence,
            bbox=job.bbox,
        )
        return _Prepared(job, rt, item)

    def _run_group(self, group: list[_Job]) -> None:
        """Judge the jobs of one group (usually one) and feed each verdict to its rule."""
        prepared = [p for p in (self._prepare(job) for job in group) if p is not None]
        if not prepared:
            return
        outcomes = self._pipeline.run([p.item for p in prepared])
        for entry, outcome in zip(prepared, outcomes, strict=True):
            if self._stop.is_set():
                return  # unloaded while the model was thinking: no store writes, no alerts
            if outcome.budget_refused:
                entry.rt.budget_skips += 1
            elif outcome.verdict is not None:
                self._apply_verdict(entry.rt, entry.job, outcome.verdict)

    def _apply_verdict(self, rt: _RuleRuntime, job: _Job, verdict: Verdict) -> None:
        """Run the optional second opinion, record the verdict and advance the rule."""
        spec = rt.spec
        verdict = self._second_opinion.review(spec, rt.machine.state, job.frame, job.bbox, verdict)
        if self._stop.is_set():
            return
        now = self._clock()
        rt.checks += 1
        rt.last_verdict = verdict
        rt.last_judged_at = self._wall()
        rt.last_reason = job.reason
        if not verdict.ok:
            self._judge_failed(rt, verdict, now)
            return
        rt.consecutive_errors = 0
        rt.judge_backoff_until = 0.0
        self._judge_succeeded(rt.source_key, now)
        step = rt.machine.observe(verdict.match, verdict.confidence, now)
        if step.state is RuleState.CANDIDATE:
            rt.next_confirm_at = now + self._confirm_spacing(spec)
        if step.fired is not None:
            self._fire(rt, job, verdict)

    def _judge_failed(self, rt: _RuleRuntime, verdict: Verdict, now: float) -> None:
        rt.errors += 1
        rt.consecutive_errors += 1
        backoff = min(
            self.settings.judge_backoff_max,
            self.settings.judge_backoff_initial * (2 ** min(rt.consecutive_errors - 1, 30)),
        )
        rt.judge_backoff_until = now + backoff
        self.last_error = _short_error(verdict.error or "judge failed")
        state = self._judge_errors.setdefault(rt.source_key, [False, None])
        state[1] = None  # a failure ends the success streak
        if not state[0]:
            state[0] = True
            self.alerter.notice(
                f"Vision checks are failing ({self.last_error}). The camera keeps being "
                "watched locally and checks resume automatically.",
                origin=rt.spec.origin,
            )

    def _judge_succeeded(self, source_key: str, now: float) -> None:
        """Re-arm the judge-error notice only after ``judge_error_rearm`` seconds of
        uninterrupted success, so a flaky model cannot send a notice per failure episode."""
        state = self._judge_errors.get(source_key)
        if state is None or not state[0]:
            return
        if state[1] is None:
            state[1] = now
        elif now - state[1] >= self.settings.judge_error_rearm:
            del self._judge_errors[source_key]

    def _fire(self, rt: _RuleRuntime, job: _Job, verdict: Verdict) -> None:
        spec = rt.spec
        source_name = rt.source_key
        wall = self._wall()
        snapshot = None
        jpeg = encode_jpeg(job.frame)
        if jpeg is not None:
            try:
                snapshot = self.store.save_snapshot(jpeg, ts=wall, rule_id=spec.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sentinel: could not save snapshot: %s", exc)
        caption = verdict.caption or f"Rule matched: {spec.question}"
        try:
            event = self.store.add_event(
                spec.id,
                caption=caption,
                reason=verdict.reason,
                confidence=verdict.confidence,
                source=source_name,
                snapshot_path=snapshot,
                ts=wall,
            )
        except Exception as exc:  # noqa: BLE001 - still alert even if the history write fails
            logger.warning("sentinel: could not store event: %s", exc)
            from .storage import EventRecord, to_iso

            event = EventRecord(
                id=0,
                rule_id=spec.id,
                ts=to_iso(wall),
                caption=caption,
                reason=verdict.reason,
                confidence=verdict.confidence,
                source=source_name,
                snapshot_path=str(snapshot) if snapshot else None,
                feedback=None,
            )
        rt.last_event_id = event.id
        self._fired += 1
        self.alerter.alert_event(
            spec, event, source_name=source_name, include_media=self.config.alert_snapshots
        )

    # ---- scheduler --------------------------------------------------------------------

    def maintenance(self) -> None:
        """Periodic housekeeping (the scheduler thread calls this once a second)."""
        now = self._clock()
        with self._lock:
            runtimes = list(self._rules.values())
        for rt in runtimes:
            try:
                rt.machine.poll(now)
            except Exception as exc:  # noqa: BLE001
                logger.debug("sentinel: poll failed for %s: %s", rt.spec.id, exc)
        try:
            self.store.prune_if_due()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentinel: retention prune failed: %s", exc)
        if now >= self._next_sync:
            self._next_sync = now + self.settings.sync_interval
            self.sync_from_store()

    def _scheduler_loop(self) -> None:
        self._next_sync = self._clock() + self.settings.sync_interval
        while not self._stop.wait(self.settings.scheduler_interval):
            try:
                self.maintenance()
            except Exception as exc:  # noqa: BLE001
                logger.exception("sentinel: maintenance failed")
                self.last_error = _short_error(exc)

    # ---- on-demand frames ---------------------------------------------------------------

    def grab_frame(self, spec: str) -> Any:
        """A current frame of ``spec``: the capture thread's latest one if fresh, else a
        one-off open/read/close. Raises :class:`SourceError` with a friendly message."""
        key = self._source_key(spec)
        with self._lock:
            worker = self._workers.get(key)
        stale = None
        if worker is not None:
            with worker.latest_lock:
                latest = worker.latest
            if latest is not None:
                if self._clock() - latest[0] <= self.settings.max_frame_age:
                    return latest[1]
                stale = latest[1]
        source = None
        try:
            source = self._source_factory(spec)
            source.open()
            for _ in range(3):
                try:
                    _ts, frame = source.read()
                    return frame
                except SourceNotReady:
                    time.sleep(0.2)
            raise SourceNotReady(f"{key}: no frame available yet")
        except Exception as exc:  # noqa: BLE001
            if stale is not None:
                return stale
            if isinstance(exc, SourceError):
                raise
            raise SourceError(f"{key}: {_short_error(exc)}") from exc
        finally:
            if source is not None:
                with suppress(Exception):
                    source.close()

    def test_rule(self, rule_id: str) -> dict[str, Any]:
        """Judge the rule once on a fresh frame without feeding its state machine."""
        with self._lock:
            rt = self._rules.get(rule_id)
            spec = rt.spec if rt is not None else None
        if spec is None:
            try:
                spec = self.store.get_rule(rule_id)
            except Exception:  # noqa: BLE001
                spec = None
        if spec is None:
            return {"ok": False, "error": f"no rule with id {rule_id!r}"}
        try:
            frame = self.grab_frame(spec.source)
        except SourceError as exc:
            return {"ok": False, "error": f"could not read {spec.source}: {_short_error(exc)}"}
        if not self._reserve_budget(spec):
            return {"ok": False, "error": self.budget.status().describe()}
        verdict = self.judge.judge(
            frame,
            spec.question,
            rule_id=spec.id,
            roi=spec.roi,
            examples=self._load_examples(spec.id),
        )
        out = {"ok": verdict.ok, "rule_id": spec.id, "question": spec.question, **verdict.to_dict()}
        if verdict.ok:
            evidence = (not verdict.match) if spec.mode == "absence" else verdict.match
            out["would_count"] = bool(evidence and verdict.confidence >= spec.confidence_threshold)
        return out

    # ---- status -----------------------------------------------------------------------

    def rule_status(self, rule_id: str) -> dict[str, Any] | None:
        with self._lock:
            rt = self._rules.get(rule_id)
            if rt is None:
                return None
            worker = self._workers.get(rt.source_key)
            online = worker.online if worker is not None else None
            source_error = worker.last_error if worker is not None else ""
        snap = rt.machine.snapshot()
        verdict = rt.last_verdict
        return {
            **snap,
            "source_key": rt.source_key,
            "source_online": online,
            "source_error": source_error,
            "checks": rt.checks,
            "errors": rt.errors,
            "budget_skips": rt.budget_skips,
            "dropped": rt.dropped,
            "last_verdict": verdict.to_dict() if verdict is not None else None,
            "last_checked": (
                datetime.fromtimestamp(rt.last_judged_at).astimezone().isoformat(timespec="seconds")
                if rt.last_judged_at
                else None
            ),
            "last_reason": rt.last_reason,
            "judging_paused_for": max(0.0, rt.judge_backoff_until - self._clock()),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            sources = [
                {
                    "source": w.key,
                    "online": w.online,
                    "error": w.last_error,
                    "frames": w.frames,
                    "rules": sorted(w.rule_ids),
                    "retry_in": max(0.0, w.next_attempt - self._clock()) if not w.is_open else 0,
                }
                for w in self._workers.values()
            ]
            rules = len(self._rules)
            active = sum(1 for r in self._rules.values() if r.spec.enabled)
        return {
            "running": self._running,
            "standing_by": self._standing_by,
            "rules": rules,
            "active_rules": active,
            "sources": sorted(sources, key=lambda s: s["source"]),
            "queue": self.queue_length(),
            "dropped_jobs": self._dropped,
            "events_fired": self._fired,
            "alerts": self.alerter.stats(),
            "last_error": self.last_error,
        }
