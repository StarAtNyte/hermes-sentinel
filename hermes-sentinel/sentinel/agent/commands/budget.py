"""``/sentinel budget``: how many vision checks are used and left."""

from __future__ import annotations

from ...runtime import Sentinel

__all__ = ["describe_budget"]


def describe_budget(sentinel: Sentinel) -> str:
    """One line with the hourly and daily vision call usage."""
    return sentinel.budget.status().describe()
