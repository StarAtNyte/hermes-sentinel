"""Per-load wiring: the :class:`Sentinel` service graph of one plugin context.

One :class:`Sentinel` owns the store, budget, judge, alerter and engine. ``register(ctx)``
builds it; tools and commands use it.

Everything heavy is lazy: the SQLite store opens on first use, the engine (and its threads)
starts on the first rule or, at load, only when enabled rules already exist. ``shutdown()``
(wired to ``ctx.on_unload``) stops the engine and closes the store. The dashboard API finds the
loaded instance through :func:`active_sentinel` of this module.
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
from .service import (
    BackgroundPruner,
    EngineWiring,
    active_sentinel,
    clear_active_sentinel,
    register_aux_tasks,
    set_active_sentinel,
)
from .sources import clear_cached_secrets, prime_secrets, referenced_secrets
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


class Sentinel:
    """Lazily built service graph for one plugin context. Thread-safe."""

    _store: Store | None = None
    _budget: Budget | None = None
    _engine: Engine | None = None
    _routes: RouteBook | None = None
    _closed = False
    _boot_timer: threading.Timer | None = None

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
        """Build the (cheap) shell; the store, budget and engine are created on first use."""
        self.ctx = ctx
        self.config = config or SentinelConfig.load(getattr(ctx, "get_config", None))
        self._store_factory = store_factory or self._default_store
        self._wiring = EngineWiring(
            source_factory=source_factory,
            source_key=source_key,
            gate_factory=gate_factory,
            settings=settings,
            clock=clock,
            wall_clock=wall_clock,
            threaded=threaded,
            use_process_lock=use_process_lock,
        )
        self._wall = wall_clock
        self._lock = threading.RLock()  # guards the lazy services below and the closed flag
        self._pruner = BackgroundPruner()
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
        self._pruner.kick(store, self._engine)
        return store

    @property
    def budget(self) -> Budget:
        """The vision-call budget over the store (built on first use)."""
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
        """The watch engine (built on first use; not started until a rule needs it)."""
        with self._lock:
            if self._engine is None:
                store = self.store
                self._engine = self._wiring.build_engine(
                    store=store,
                    judge=self.judge,
                    alerter=self.alerter,
                    budget=self.budget,
                    config=self.config,
                    lock_filename=LOCK_FILENAME,
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
        """The engine if something already built it, else ``None`` (never builds one)."""
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

    def prime_secrets(self) -> None:
        """Remember the ``${SECRET}`` values sources use, while a secret scope is active.

        Called at the start of every tool and command call. On a multiplexing gateway only
        those calls run inside the scope; the values go to an in-memory cache (never to disk)
        that the capture threads fall back on. Never raises.
        """
        try:
            sources = list(self.config.stream_urls.values())
            sources += [rule.source for rule in self.store.list_rules(enabled_only=True)]
            prime_secrets(referenced_secrets(sources))
        except Exception as exc:  # noqa: BLE001 - priming is best effort
            logger.debug("sentinel: priming secrets failed: %s", exc)

    def housekeeping(self) -> None:
        """Apply retention (``store.prune_if_due``) even when no rule is enabled. Never raises."""
        try:
            self.store.prune_if_due()
        except Exception as exc:  # noqa: BLE001
            logger.debug("sentinel: housekeeping prune failed: %s", exc)

    def shutdown(self) -> None:
        """Stop threads and close the store (``ctx.on_unload``). Idempotent, never raises."""
        with self._lock:
            self._closed = True
            timer, self._boot_timer = self._boot_timer, None
            engine, store = self._engine, self._store
            self._engine = None
            self._store = None
            self._budget = None
        clear_cached_secrets()
        clear_active_sentinel(self)
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
