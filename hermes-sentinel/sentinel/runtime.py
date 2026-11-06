"""Per-load wiring: one :class:`Sentinel` object owns the store, budget, judge, alerter and
engine for a plugin context. ``register(ctx)`` builds it; tools and commands use it.

Everything heavy is lazy: the SQLite store opens on first use, the engine (and its threads)
starts on the first rule or, at load, only when enabled rules already exist. ``shutdown()``
(wired to ``ctx.on_unload``) stops the engine and closes the store.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .alerts import Alerter, RouteBook
from .budget import Budget
from .config import SentinelConfig
from .engine import Engine, EngineSettings
from .storage import Store, StoreError
from .vision import Judge

logger = logging.getLogger(__name__)

__all__ = [
    "LOCK_FILENAME",
    "Sentinel",
    "StoreError",
    "active_sentinel",
    "register_aux_tasks",
    "set_active_sentinel",
]

LOCK_FILENAME = "engine.lock"
BOOT_DELAY_SECONDS = 25.0  # short-lived hermes commands exit before the cameras ever open
ALERTS_PER_HOUR = 20  # every injected alert is a full agent turn on the main model
NOTICES_PER_HOUR = 6
ROUTES_FILENAME = "routes.json"


_ACTIVE: Sentinel | None = None
_ACTIVE_LOCK = threading.Lock()


def set_active_sentinel(sentinel: Sentinel | None) -> None:
    """Publish the loaded Sentinel so the dashboard API (a separately imported module) can
    reach the same store/engine the tools use. ``None`` clears it."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = sentinel


def active_sentinel() -> Sentinel | None:
    with _ACTIVE_LOCK:
        return _ACTIVE


def register_aux_tasks(ctx: Any) -> list[str]:
    """Register the plugin's two LLM tasks (vision judge, rule compiler). Never raises."""
    from .rules import RULES_TASK, RULES_TASK_INFO
    from .vision import VISION_TASK, VISION_TASK_INFO

    done = []
    for key, info in ((VISION_TASK, VISION_TASK_INFO), (RULES_TASK, RULES_TASK_INFO)):
        try:
            ctx.register_auxiliary_task(key, **info)
            done.append(key)
        except Exception as exc:  # noqa: BLE001 - a host without aux tasks still gets tools
            logger.warning("sentinel: could not register auxiliary task %s: %s", key, exc)
    return done


class Sentinel:
    """Lazily built service graph for one plugin context. Thread-safe."""

    def __init__(
        self,
        ctx: Any,
        *,
        config: SentinelConfig | None = None,
        store_factory: Callable[[SentinelConfig], Store] | None = None,
        source_factory: Callable[[str], Any] | None = None,
        source_key: Callable[[str], str] | None = None,
        gate_factory: Callable[[Any], Any] | None = None,
        settings: EngineSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        threaded: bool = True,
        use_process_lock: bool = True,
    ) -> None:
        self.ctx = ctx
        self.config = config or SentinelConfig.load(getattr(ctx, "get_config", None))
        self._store_factory = store_factory or self._default_store
        self._source_factory = source_factory
        self._source_key = source_key
        self._gate_factory = gate_factory
        self._settings = settings
        self._clock = clock
        self._wall = wall_clock
        self._threaded = threaded
        self._use_process_lock = use_process_lock
        self._lock = threading.RLock()
        self._store: Store | None = None
        self._budget: Budget | None = None
        self._engine: Engine | None = None
        self._routes: RouteBook | None = None
        self._closed = False
        self._boot_timer: threading.Timer | None = None
        self._pruning = False
        self.judge = Judge(lambda: getattr(ctx, "llm", None), clock=clock)
        self.alerter = Alerter(
            ctx,
            clock=clock,
            include_media=self.config.alert_snapshots,
            global_per_hour=ALERTS_PER_HOUR,
            notices_per_hour=NOTICES_PER_HOUR,
        )

    def _default_store(self, cfg: SentinelConfig) -> Store:
        return Store(
            retention_days=cfg.retention_days,
            max_storage_mb=cfg.max_storage_mb,
            clock=self._wall,
        )

    # ---- lazy services ----------------------------------------------------------------

    @property
    def store(self) -> Store:
        """The event store (opened on first use). Raises :class:`StoreError`."""
        with self._lock:
            if self._closed:
                raise StoreError("Sentinel is shutting down")
            if self._store is None:
                self._store = self._store_factory(self.config)
            store = self._store
        self._kick_prune(store)
        return store

    @property
    def budget(self) -> Budget:
        with self._lock:
            if self._budget is None:
                self._budget = Budget(
                    self.store,
                    per_hour=self.config.vision_calls_per_hour,
                    per_day=self.config.vision_calls_per_day,
                    clock=self._wall,
                )
            return self._budget

    @property
    def engine(self) -> Engine:
        with self._lock:
            if self._engine is None:
                store = self.store
                self._engine = Engine(
                    store=store,
                    judge=self.judge,
                    alerter=self.alerter,
                    budget=self.budget,
                    config=self.config,
                    settings=self._settings,
                    source_factory=self._source_factory,
                    source_key=self._source_key,
                    gate_factory=self._gate_factory,
                    clock=self._clock,
                    wall_clock=self._wall,
                    lock_path=store.data_dir / LOCK_FILENAME if self._use_process_lock else None,
                    threaded=self._threaded,
                )
            return self._engine

    @property
    def routes(self) -> RouteBook:
        """Chat routes learned from ``/sentinel`` calls (persisted next to the store)."""
        with self._lock:
            if self._routes is None:
                try:
                    path = self.store.data_dir / ROUTES_FILENAME
                except Exception:  # noqa: BLE001 - no store: keep routes in memory only
                    path = None
                self._routes = RouteBook(path)
            return self._routes

    @property
    def engine_if_built(self) -> Engine | None:
        return self._engine

    # ---- lifecycle --------------------------------------------------------------------

    def boot(self) -> None:
        """Start the engine now if enabled rules exist. Never raises."""
        try:
            self.housekeeping()
            if self.store.list_rules(enabled_only=True):
                self.engine.start()
        except Exception as exc:  # noqa: BLE001 - a broken store must not break plugin load
            logger.warning("sentinel: not starting the watcher at load: %s", exc)

    def boot_delayed(self, delay: float | None = None) -> None:
        """Run :meth:`boot` after ``delay`` seconds on a cancellable daemon timer.

        Any process that merely loads the plugin (a one-shot ``hermes`` command, validate,
        doctor) exits well before this fires, so it never opens a camera, takes the engine
        lock or spends vision calls. ``shutdown()`` cancels the timer. A tool or command that
        needs the watcher sooner starts it itself (``add_rule`` starts the engine).
        """
        delay = BOOT_DELAY_SECONDS if delay is None else delay
        with self._lock:
            if self._closed or self._boot_timer is not None:
                return
            timer = threading.Timer(max(0.0, float(delay)), self._boot_if_open)
            timer.daemon = True
            timer.name = "sentinel-boot"
            self._boot_timer = timer
        timer.start()

    def _boot_if_open(self) -> None:
        if not self._closed:
            self.boot()

    def housekeeping(self) -> None:
        """Apply retention (``store.prune_if_due``) even when no rule is enabled. Never raises."""
        try:
            self.store.prune_if_due()
        except Exception as exc:  # noqa: BLE001
            logger.debug("sentinel: housekeeping prune failed: %s", exc)

    def _kick_prune(self, store: Store) -> None:
        """From tool/command entry points: prune in the background when the engine's own
        scheduler is not doing it (no engine running). Cheap no-op unless a prune is due."""
        engine = self._engine
        if self._pruning or (engine is not None and engine.running):
            return
        due = getattr(store, "prune_due", None)
        if due is not None and not due():
            return
        self._pruning = True

        def run() -> None:
            try:
                store.prune_if_due()
            except Exception as exc:  # noqa: BLE001
                logger.debug("sentinel: background prune failed: %s", exc)
            finally:
                self._pruning = False

        threading.Thread(target=run, name="sentinel-prune", daemon=True).start()

    def shutdown(self) -> None:
        """Stop threads and close the store (``ctx.on_unload``). Idempotent, never raises."""
        with self._lock:
            self._closed = True
            timer, self._boot_timer = self._boot_timer, None
            engine, store = self._engine, self._store
            self._engine = None
            self._store = None
            self._budget = None
        global _ACTIVE
        with _ACTIVE_LOCK:
            if _ACTIVE is self:
                _ACTIVE = None
        if timer is not None:
            timer.cancel()
        if engine is not None:
            try:
                engine.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sentinel: engine stop failed: %s", exc)
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sentinel: store close failed: %s", exc)
