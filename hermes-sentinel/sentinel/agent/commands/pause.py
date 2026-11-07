"""``/sentinel pause|resume <rule_id>``."""

from __future__ import annotations

from typing import Any

from ...alerts import visible_to
from ...runtime import Sentinel
from .common import USAGE

__all__ = ["set_enabled"]


def set_enabled(
    sentinel: Sentinel, rule_id: str, enabled: bool, caller: dict[str, Any] | None = None
) -> str:
    """Pause or resume one rule the caller can see; the reply says what happened."""
    if not rule_id:
        return USAGE
    spec = sentinel.store.get_rule(rule_id)
    if spec is not None and not visible_to(spec.origin, caller or {}, sentinel.routes):
        spec = None
    if spec is None:
        return f"No rule {rule_id!r}. Try /sentinel list."
    saved = sentinel.store.upsert_rule(spec.with_changes(enabled=enabled))
    if enabled:
        sentinel.engine.add_rule(saved)
    elif sentinel.engine_if_built is not None:
        sentinel.engine.update_rule(saved)
    return f"{'Resumed' if enabled else 'Paused'} rule {rule_id}."
