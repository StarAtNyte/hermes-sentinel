"""Best-effort JSON files for the small books kept next to the store."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["read_json_object", "write_json_atomic"]


def read_json_object(path: Path | None, label: str) -> dict[str, Any]:
    """The JSON object stored at ``path``; empty when missing, unreadable or not an object."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("sentinel: ignoring unreadable %s file: %s", label, exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path | None, data: Any, label: str) -> None:
    """Write ``data`` to ``path`` through a temporary file; a failure is logged, not raised."""
    if path is None:
        return
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("sentinel: could not save %s: %s", label, exc)
