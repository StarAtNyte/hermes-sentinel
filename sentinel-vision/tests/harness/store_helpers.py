"""Shared constants and helpers for the storage tests."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

store_mod = plugin_module("sentinel.storage")
rules = plugin_module("sentinel.rules")

T0 = 1_762_430_400.0  # 2025-11-06T12:00:00Z
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 1000


class Clock:
    """Mutable fake clock: ``clock.t`` is the current time."""

    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def rule(rid="rmug", **kw):
    """A validated rule for storage tests."""
    return rules.RuleSpec(question="Is the mug there?", source="cam", id=rid, **kw).validate()
