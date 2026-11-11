"""Result and input types of the vision judge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Description", "Example", "Verdict"]


@dataclass(frozen=True)
class Verdict:
    """One judgement. ``match`` is ``None`` when the judge failed (see ``error``)."""

    match: bool | None
    confidence: float = 0.0
    reason: str = ""
    caption: str = ""
    error: str | None = None
    elapsed: float = 0.0
    model: str = ""

    @property
    def ok(self) -> bool:
        """True when the judge produced a usable yes/no answer."""
        return self.error is None and self.match is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form with rounded numbers."""
        return {
            "match": self.match,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "caption": self.caption,
            "error": self.error,
            "elapsed": round(self.elapsed, 3),
            "model": self.model,
        }


@dataclass(frozen=True)
class Description:
    """Result of :meth:`Judge.describe` (a free-form look at a frame)."""

    answer: str = ""
    caption: str = ""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        """True when the description succeeded."""
        return self.error is None


@dataclass(frozen=True)
class Example:
    """A teaching example image: ``kind`` is ``"pos"`` (answer yes) or ``"neg"`` (answer no)."""

    kind: str
    data: bytes
    mime_type: str = "image/jpeg"
    note: str = ""
