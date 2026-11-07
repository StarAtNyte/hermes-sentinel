"""The JSON result and error shapes every tool returns."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["ToolError", "err", "ok"]


class ToolError(Exception):
    """A user-facing error (its message is returned as ``error``)."""


def ok(**data: Any) -> str:
    """A success result: ``{"ok": true, ...data}`` as a JSON string."""
    return json.dumps({"ok": True, **data}, default=str, ensure_ascii=False)


def err(message: str, **data: Any) -> str:
    """A failure result: ``{"ok": false, "error": message, ...data}`` as a JSON string."""
    return json.dumps({"ok": False, "error": message, **data}, default=str, ensure_ascii=False)
