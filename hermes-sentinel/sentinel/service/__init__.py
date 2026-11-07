"""Collaborators of the per-load :class:`~sentinel.runtime.Sentinel` composition root."""

from .aux_tasks import register_aux_tasks
from .engine_wiring import EngineWiring
from .pruner import BackgroundPruner
from .registry import active_sentinel, clear_active_sentinel, set_active_sentinel

__all__ = [
    "BackgroundPruner",
    "EngineWiring",
    "active_sentinel",
    "clear_active_sentinel",
    "register_aux_tasks",
    "set_active_sentinel",
]
