"""hermes-sentinel: plain-English visual watch rules for Hermes Agent.

``register(ctx)`` wires everything through public plugin APIs only:

* two auxiliary LLM tasks: ``sentinel_vision`` (the frame judge) and ``sentinel_rules`` (the
  text-only rule compiler), so users can route each to a suitable model;
* the eight agent tools of DESIGN.md section 3 (``sentinel_*``);
* the ``/sentinel`` slash command;
* ``ctx.on_unload`` to stop the watcher threads and close the store.

The watcher itself starts lazily: on the first rule, or right away at load when enabled rules
already exist. Nothing heavy (OpenCV, numpy, SQLite, devices) is touched before that.
"""

from __future__ import annotations

import logging
from typing import Any

__version__ = "0.0.1"

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Plugin entry point called by Hermes."""
    from .sentinel.commands import ARGS_HINT, SentinelCommand
    from .sentinel.runtime import Sentinel, register_aux_tasks
    from .sentinel.tools import register_tools

    register_aux_tasks(ctx)
    sentinel = Sentinel(ctx)
    register_tools(ctx, sentinel)
    ctx.register_command(
        "sentinel",
        handler=SentinelCommand(sentinel, __version__),
        description="Sentinel visual watcher: status, rules, pause/resume, test, budget",
        args_hint=ARGS_HINT,
    )
    ctx.on_unload(sentinel.shutdown)
    sentinel.boot()
