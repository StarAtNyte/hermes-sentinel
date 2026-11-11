"""Short, credential-free error text for notices and status."""

from __future__ import annotations

from ...sources import redact_url

__all__ = ["MAX_ERROR_TEXT", "short_error"]

MAX_ERROR_TEXT = 240


def short_error(exc: BaseException | str) -> str:
    """One line of ``exc`` (at most ``MAX_ERROR_TEXT`` characters) with URL credentials masked."""
    text = str(exc) if not isinstance(exc, str) else exc
    text = " ".join(text.split()) or (type(exc).__name__ if not isinstance(exc, str) else "")
    # Belt and braces: never echo a URL with credentials (sources already redact).
    words = [redact_url(w) if "://" in w else w for w in text.split(" ")]
    text = " ".join(words)
    return text[:MAX_ERROR_TEXT]
