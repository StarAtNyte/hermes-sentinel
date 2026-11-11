"""How a Sentinel builds its engine: injectable factories, clocks and threading mode."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..alerts import Alerter
from ..budget import Budget
from ..config import SentinelConfig
from ..engine import Engine, EngineSettings
from ..vision import Judge

__all__ = ["EngineWiring"]


@dataclass(frozen=True)
class EngineWiring:
    """The knobs tests inject (fake sources, gates, clocks) and the production defaults."""

    source_factory: Callable[[str], Any] | None = None
    source_key: Callable[[str], str] | None = None
    gate_factory: Callable[[Any], Any] | None = None
    settings: EngineSettings | None = None
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    threaded: bool = True
    use_process_lock: bool = True

    def build_engine(
        self,
        *,
        store: Any,
        judge: Judge,
        alerter: Alerter,
        budget: Budget,
        config: SentinelConfig,
        lock_filename: str,
    ) -> Engine:
        """An engine over the given services; its lock file lives in the store's data directory."""
        return Engine(
            store=store,
            judge=judge,
            alerter=alerter,
            budget=budget,
            config=config,
            settings=self.settings,
            source_factory=self.source_factory,
            source_key=self.source_key,
            gate_factory=self.gate_factory,
            clock=self.clock,
            wall_clock=self.wall_clock,
            lock_path=store.data_dir / lock_filename if self.use_process_lock else None,
            threaded=self.threaded,
        )
