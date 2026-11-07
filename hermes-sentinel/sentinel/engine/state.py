"""The one state object every engine collaborator shares."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..alerts import Alerter
from ..budget import Budget
from ..config import SentinelConfig
from ..judging import JudgingPolicy
from ..sources import open_source
from ..vision import Judge
from .infra import default_source_key, short_error
from .models import RuleRuntime, SourceWorker
from .settings import EngineSettings

__all__ = ["EngineState"]


class EngineState:
    """Services, clocks, flags and the rule/worker registries of one engine.

    Collaborators (capture, queue, runner, lifecycle, ...) hold a reference to this object
    instead of to each other's internals. Locking:

    * ``lock`` (re-entrant) guards ``rules``, ``workers``, the thread handles owned by the
      lifecycle and the ``running`` / ``standing_by`` / ``shutdown`` flags. Nothing blocking
      (camera, model, network) runs while it is held, except the start-up store read and the
      process-lock attempt that make ``start()`` atomic against ``stop()``.
    * The job queue has its own condition (see :class:`jobs.JobQueue`); a rule's ``queued`` and
      ``in_flight`` fields belong to that condition, not to ``lock``.
    * ``stop`` is the cross-thread stop signal; ``shutdown`` records that ``stop()`` ran, so an
      engine is never restarted.
    """

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
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        threaded: bool = True,
    ) -> None:
        """Bundle the engine's services; ``clock`` is monotonic, ``wall_clock`` is epoch."""
        self.store = store
        self.judge = judge
        self.alerter = alerter
        self.budget = budget
        self.config = config
        self.settings = settings or EngineSettings()
        self.source_factory = source_factory or self._open_default
        self.source_key = source_key or self._default_key
        self.clock = clock
        self.wall = wall_clock
        self.threaded = bool(threaded)
        self.policy = JudgingPolicy.from_config(config)
        self.lock = threading.RLock()  # guards rules, workers, thread handles and the flags below
        self.rules: dict[str, RuleRuntime] = {}
        self.workers: dict[str, SourceWorker] = {}
        self.stop = threading.Event()
        self.running = False
        self.standing_by = False
        self.shutdown = False
        self.fired = 0
        self.last_error = ""
        self.started_at: float | None = None  # wall clock of the last successful start

    def _open_default(self, spec: str) -> Any:
        return open_source(spec, named_streams=self.config.stream_urls)

    def _default_key(self, spec: str) -> str:
        return default_source_key(spec, self.config.stream_urls)

    def record_error(self, error: BaseException | str) -> None:
        """Remember ``error`` (shortened, credentials masked) as the engine's last error."""
        self.last_error = short_error(error)
