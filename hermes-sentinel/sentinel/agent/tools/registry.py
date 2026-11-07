"""``SentinelTools``: binds the tool modules to one Sentinel and registers them with Hermes.

Every handler has the Hermes signature ``handler(args: dict, **kwargs) -> str`` and returns a
JSON object: ``{"ok": true, ...}`` on success or ``{"ok": false, "error": "<friendly text>"}``.
Handlers never raise; unexpected failures are logged and reported as a short error.

``kwargs`` carries the calling session (``session_id``, ``task_id``, ...); ``sentinel_watch``
stores it (plus the session's routing key) on the rule so alerts reach the same chat later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from ...rules import RuleSpec
from ...runtime import Sentinel
from .catalog import EMOJI, SCHEMAS, TOOL_NAMES, TOOLSET
from .common import HourlyCap, OncePerKey, ToolContext
from .dispatch import wrap_handler
from .inspection import events, look, setup_check, sources
from .management import feedback, list_rules, teach, update, watch

__all__ = ["COMPILE_CALLS_PER_HOUR", "SentinelTools", "register_tools"]

#: Plain-English rule compiling costs an LLM call outside the vision budget: small own cap.
COMPILE_CALLS_PER_HOUR = 10


class SentinelTools:
    """The nine tool handlers, bound to one :class:`Sentinel`."""

    def __init__(
        self,
        sentinel: Sentinel,
        *,
        compile_per_hour: int = COMPILE_CALLS_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
        owner_origin: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind to ``sentinel``; ``owner_origin`` makes every call act as that fixed origin."""
        self.s = sentinel
        self.context = ToolContext(
            sentinel=sentinel,
            owner_origin=dict(owner_origin) if owner_origin is not None else None,
            compile_cap=HourlyCap(compile_per_hour, clock),
            here_hints=OncePerKey(),
        )

    def handlers(self) -> dict[str, Callable[..., str]]:
        """Handlers by tool name, each wrapped so it never raises."""
        return {
            name: wrap_handler(name, SCHEMAS[name]["description"], getattr(self, name), self.s)
            for name in TOOL_NAMES
        }

    @staticmethod
    def status_text(spec: RuleSpec, engine: Any, status: Mapping[str, Any] | None) -> str:
        """Human status of a rule: paused, not running, starting, source offline, or state."""
        return list_rules.status_text(spec, engine, status)

    def sentinel_sources(self, args: dict, **kwargs: Any) -> str:
        """List the cameras, monitors and streams that can be watched."""
        return sources.run(self.context, args, kwargs)

    def sentinel_setup_check(self, args: dict, **kwargs: Any) -> str:
        """Run the readiness check, optionally with the delivery self-test."""
        return setup_check.run(self.context, args, kwargs)

    def sentinel_watch(self, args: dict, **kwargs: Any) -> str:
        """Start watching a source for something described in plain English."""
        return watch.run(self.context, args, kwargs)

    def sentinel_rules(self, args: dict, **kwargs: Any) -> str:
        """List watch rules with their status."""
        return list_rules.run(self.context, args, kwargs)

    def sentinel_update(self, args: dict, **kwargs: Any) -> str:
        """Pause, resume, delete or edit a rule."""
        return update.run(self.context, args, kwargs)

    def sentinel_look(self, args: dict, **kwargs: Any) -> str:
        """Grab a frame now and describe it."""
        return look.run(self.context, args, kwargs)

    def sentinel_events(self, args: dict, **kwargs: Any) -> str:
        """Search confirmed events."""
        return events.run(self.context, args, kwargs)

    def sentinel_feedback(self, args: dict, **kwargs: Any) -> str:
        """Mark an alert correct or a false alarm."""
        return feedback.run(self.context, args, kwargs)

    def sentinel_teach(self, args: dict, **kwargs: Any) -> str:
        """Add a positive or negative example image to a rule."""
        return teach.run(self.context, args, kwargs)


def register_tools(ctx: Any, sentinel: Sentinel) -> list[str]:
    """Register all tools on ``ctx``; returns the names registered."""
    handlers = SentinelTools(sentinel).handlers()
    names = []
    for name in TOOL_NAMES:
        handle = ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=SCHEMAS[name],
            handler=handlers[name],
            description=SCHEMAS[name]["description"],
            emoji=EMOJI.get(name, ""),
        )
        if handle is not None:
            names.append(name)
    return names
