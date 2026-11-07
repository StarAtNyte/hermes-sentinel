"""The nine tools in registration order, with their schemas and emoji."""

from __future__ import annotations

from typing import Any

from .common import ToolSpec
from .inspection import events, look, setup_check, sources
from .management import feedback, list_rules, teach, update, watch

__all__ = ["EMOJI", "SCHEMAS", "TOOLSET", "TOOL_NAMES", "TOOL_SPECS"]

TOOLSET = "sentinel"

TOOL_SPECS: tuple[ToolSpec, ...] = (
    sources.SPEC,
    watch.SPEC,
    list_rules.SPEC,
    update.SPEC,
    look.SPEC,
    events.SPEC,
    feedback.SPEC,
    teach.SPEC,
    setup_check.SPEC,
)
SCHEMAS: dict[str, dict[str, Any]] = {spec.name: spec.schema for spec in TOOL_SPECS}
EMOJI: dict[str, str] = {spec.name: spec.emoji for spec in TOOL_SPECS}
TOOL_NAMES: tuple[str, ...] = tuple(SCHEMAS)
