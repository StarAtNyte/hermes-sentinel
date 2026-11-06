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
from .judge import Judge
from .store import Store, StoreError

logger = logging.getLogger(__name__)

__all__ = ["LOCK_FILENAME", "Sentinel", "StoreError", "register_aux_tasks"]

LOCK_FILENAME = "engine.lock"
ROUTES_FILENAME = "routes.json"


def register_aux_tasks(ctx: Any) -> list[str]:
    """Register the plugin's two LLM tasks (vision judge, rule compiler). Never raises."""
    from .judge import VISION_TASK, VISION_TASK_INFO
    from .rules import RULES_TASK, RULES_TASK_INFO

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
        self.judge = Judge(lambda: getattr(ctx, "llm", None), clock=clock)
        self.alerter = Alerter(ctx, clock=clock, include_media=self.config.alert_snapshots)

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
            return self._store

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
        """Start the engine at load time if enabled rules exist. Never raises."""
        try:
            if self.store.list_rules(enabled_only=True):
                self.engine.start()
        except Exception as exc:  # noqa: BLE001 - a broken store must not break plugin load
            logger.warning("sentinel: not starting the watcher at load: %s", exc)

    def shutdown(self) -> None:
        """Stop threads and close the store (``ctx.on_unload``). Idempotent, never raises."""
        with self._lock:
            self._closed = True
            engine, store = self._engine, self._store
            self._engine = None
            self._store = None
            self._budget = None
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
