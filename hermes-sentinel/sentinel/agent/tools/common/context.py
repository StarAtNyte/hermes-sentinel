"""What every tool handler works with: the Sentinel, the caller's origin and shared throttles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ....alerts import capture_origin, is_chat_origin, visible_to
from ....rules import RuleSpec
from ....runtime import Sentinel
from ....sources import SourceSpec
from ....storage import EventRecord
from .args import str_arg
from .results import ToolError
from .throttles import HourlyCap, OncePerKey

__all__ = ["ToolContext", "ToolSpec"]


@dataclass
class ToolContext:
    """One Sentinel plus the per-registry state that tool handlers share.

    ``owner_origin``: when set, every call acts as this fixed (local) origin; the dashboard
    API uses it. ``compile_cap`` limits plain-English rule compiling (an LLM call outside the
    vision budget); ``here_hints`` makes the ``/sentinel here`` hint appear once per session.
    """

    sentinel: Sentinel
    owner_origin: dict[str, Any] | None
    compile_cap: HourlyCap
    here_hints: OncePerKey

    def source_arg(self, args: Mapping[str, Any]) -> str:
        """The validated ``source`` argument, defaulting to the configured default source."""
        config = self.sentinel.config
        source = str_arg(args, "source", limit=2048) or config.default_source
        SourceSpec.parse(source, named_streams=config.stream_urls)  # validate early
        return source

    def caller(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The calling session's origin (``capture_origin``): who may see which rules."""
        if self.owner_origin is not None:
            return dict(self.owner_origin)
        return capture_origin(kwargs, self.sentinel.ctx, routes=self.sentinel.routes)

    def rule(self, rule_id: str, caller: Mapping[str, Any] | None = None) -> RuleSpec:
        """The rule ``rule_id``; another chat's rule is reported as missing, not forbidden."""
        spec = self.sentinel.store.get_rule(rule_id)
        if (
            spec is not None
            and caller is not None
            and not visible_to(spec.origin, caller, self.sentinel.routes)
        ):
            spec = None
        if spec is None:
            raise ToolError(f"no rule with id {rule_id!r}; use sentinel_rules to list rules")
        return spec

    def owns_event(self, event: EventRecord, caller: Mapping[str, Any]) -> bool:
        """Whether ``caller`` may touch ``event``: messaging chats only reach their own rules."""
        if not is_chat_origin(caller):
            return True
        owner = self.sentinel.store.get_rule(event.rule_id)
        return owner is not None and visible_to(owner.origin, caller, self.sentinel.routes)

    def save_and_apply(self, spec: RuleSpec) -> RuleSpec:
        """Persist ``spec`` and push it to the engine (added when enabled, else updated)."""
        saved = self.sentinel.store.upsert_rule(spec)
        engine = self.sentinel.engine
        if saved.enabled:
            engine.add_rule(saved)
        else:
            engine.update_rule(saved)
        return saved


@dataclass(frozen=True)
class ToolSpec:
    """A tool's name, schema, emoji and handler body, defined next to each other."""

    name: str
    schema: dict[str, Any]
    emoji: str
    run: Callable[[ToolContext, dict[str, Any], Mapping[str, Any]], str]
