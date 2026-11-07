"""``/sentinel test <rule_id>``: judge one live frame (one vision call, counted in the budget)."""

from __future__ import annotations

from typing import Any

from ...alerts import visible_to
from ...runtime import Sentinel
from .common import USAGE

__all__ = ["probe_rule"]


def probe_rule(sentinel: Sentinel, rule_id: str, caller: dict[str, Any] | None = None) -> str:
    """Run the rule against its source now and describe the verdict."""
    if not rule_id:
        return USAGE
    spec = sentinel.store.get_rule(rule_id)
    if spec is not None and not visible_to(spec.origin, caller or {}, sentinel.routes):
        return f"Test of {rule_id} failed: no such rule"
    result = sentinel.engine.test_rule(rule_id)
    if not result.get("ok"):
        return f"Test of {rule_id} failed: {result.get('error')}"
    answer = "yes" if result["match"] else "no"
    counts = "would count as evidence" if result.get("would_count") else "would not count"
    return (
        f"{rule_id}: {result['question']} -> {answer} (confidence "
        f"{result['confidence']:.2f}; {counts}). {result.get('caption') or ''}"
    ).strip()
