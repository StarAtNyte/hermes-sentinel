"""The result type shared by every setup check."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["FAIL", "OK", "WARN", "Check"]

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    """One line of the readiness report: what was checked, how it went and how to fix it."""

    id: str
    title: str
    status: str  # ok | warn | fail
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        """The check as a plain JSON-ready mapping."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }
