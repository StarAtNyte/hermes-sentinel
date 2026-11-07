"""Everything the setup checks look at, injectable so tests pass fakes."""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ...alerts import route_from_kwargs
from ...sources import list_sources

__all__ = ["SetupEnv"]


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _probe(**kwargs: Any) -> list[Any]:
    return list_sources(**kwargs)


@dataclass
class SetupEnv:
    """Everything the checks look at. Defaults are cheap; ``from_sentinel`` fills real ones."""

    config: Any = None  # SentinelConfig
    has_llm: bool | None = None  # ctx.llm present?
    store_getter: Callable[[], Any] | None = None  # -> Store (may raise)
    engine: Any = None  # Engine or None when never built
    alerter: Any = None
    budget_getter: Callable[[], Any] | None = None
    route: Mapping[str, Any] | None = None  # None: not applicable (no session)
    probe_sources: bool = True
    module_available: Callable[[str], bool] = _find_spec
    list_sources: Callable[..., list[Any]] = _probe
    disk_free_mb: Callable[[Any], float] = field(
        default=lambda path: shutil.disk_usage(path).free / (1024 * 1024)
    )

    @classmethod
    def from_sentinel(
        cls, sentinel: Any, kwargs: Mapping[str, Any] | None = None, *, probe_sources: bool = True
    ) -> SetupEnv:
        """Build from a live :class:`Sentinel`. ``kwargs``: the calling session, if any."""
        route: dict[str, Any] | None = None
        if kwargs is not None:
            try:
                route = route_from_kwargs(kwargs)
                if not route.get("session_key"):
                    route = sentinel.routes.lookup(str(kwargs.get("session_id") or "")) or route
            except Exception:  # noqa: BLE001
                route = {}
        engine = sentinel.engine_if_built
        if engine is not None and getattr(engine, "running", False):
            probe_sources = False  # never fight the watcher for a camera
        return cls(
            config=sentinel.config,
            has_llm=getattr(sentinel.ctx, "llm", None) is not None,
            store_getter=lambda: sentinel.store,
            engine=engine,
            alerter=sentinel.alerter,
            budget_getter=lambda: sentinel.budget,
            route=route,
            probe_sources=probe_sources,
        )
