"""Summaries and the plain-text report built from a list of checks."""

from __future__ import annotations

from typing import Any

from .model import FAIL, OK, WARN, Check

__all__ = ["fail_titles", "format_report", "summarize"]


def summarize(checks: list[Check]) -> dict[str, Any]:
    """``ready`` is true when nothing failed; ``to_fix`` counts every non-ok check."""
    failed = sum(1 for c in checks if c.status == FAIL)
    to_fix = sum(1 for c in checks if c.status != OK)
    return {"ready": failed == 0, "failed": failed, "to_fix": to_fix}


def fail_titles(checks: list[Check]) -> list[str]:
    """Titles of the checks that failed."""
    return [c.title for c in checks if c.status == FAIL]


def _ready_line(summary: dict[str, Any]) -> str:
    """The closing verdict: failures are "to fix", warnings alone are "to review"."""
    count = summary["to_fix"]
    if not summary["ready"]:
        return f"Ready: no - {count} {'thing' if count == 1 else 'things'} to fix"
    if count == 0:
        return "Ready: yes - nothing to fix"
    return f"Ready: yes - {count} {'item' if count == 1 else 'items'} to review"


NEXT_STEP = (
    "Next: tell Hermes what to watch, for example "
    '"watch screen:1 and tell me when the build page shows a red Build failed banner".'
)


def format_report(checks: list[Check]) -> str:
    """The multi-line report: one line per check, its fix when not ok, then the verdict."""
    marks = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = ["Sentinel setup check"]
    for c in checks:
        lines.append(f"{marks.get(c.status, '[?]   ')} {c.title}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append("       fix: " + c.fix.replace("\n", "\n            "))
    s = summarize(checks)
    if s["ready"]:
        lines.append(NEXT_STEP)
    lines.append(_ready_line(s))
    return "\n".join(lines)
