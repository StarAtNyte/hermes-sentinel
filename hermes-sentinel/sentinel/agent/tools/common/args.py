"""Typed readers for tool arguments; each raises :class:`ToolError` with a friendly message."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ....rules import RuleValidationError, Schedule
from .results import ToolError

__all__ = [
    "MAX_TEXT",
    "bool_arg",
    "int_arg",
    "num_arg",
    "parse_roi",
    "parse_schedule",
    "str_arg",
]

MAX_TEXT = 2000


def str_arg(
    args: Mapping[str, Any], key: str, *, required: bool = False, limit: int = MAX_TEXT
) -> str:
    """A stripped string argument, ``""`` when absent (unless ``required``)."""
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolError(f"'{key}' is required")
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ToolError(f"'{key}' must be a string")
    text = str(value).strip()
    if len(text) > limit:
        raise ToolError(f"'{key}' is too long (max {limit} characters)")
    return text


def int_arg(args: Mapping[str, Any], key: str, *, required: bool = False) -> int | None:
    """An integer argument (``#12`` and ``12.0`` are accepted), ``None`` when absent."""
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if isinstance(value, bool):
        raise ToolError(f"'{key}' must be an integer")
    try:
        number = float(str(value).strip().lstrip("#"))
    except ValueError:
        raise ToolError(f"'{key}' must be an integer") from None
    if not math.isfinite(number) or number != int(number):
        raise ToolError(f"'{key}' must be an integer")
    return int(number)


def num_arg(args: Mapping[str, Any], key: str) -> float | None:
    """A finite, non-negative number argument, ``None`` when absent."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ToolError(f"'{key}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{key}' must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise ToolError(f"'{key}' must be a non-negative number")
    return number


def bool_arg(args: Mapping[str, Any], key: str) -> bool:
    """A strict boolean argument (anything but true/false is an error)."""
    value = args.get(key)
    if not isinstance(value, bool):
        raise ToolError(f"'{key}' must be true or false")
    return value


def parse_roi(value: Any) -> tuple[float, float, float, float] | None:
    """A region of interest ``[x, y, w, h]`` in frame fractions, ``None`` when absent."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ToolError("roi must be [x, y, width, height] as fractions between 0 and 1")
    try:
        roi = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        raise ToolError("roi values must be numbers") from None
    x, y, w, h = roi
    if (
        not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1)
        or x + w > 1.0001
        or (y + h > 1.0001)
    ):
        raise ToolError("roi must be fractions inside the frame, e.g. [0.25, 0.25, 0.5, 0.5]")
    return roi  # type: ignore[return-value]


def parse_schedule(value: Any) -> Schedule | None:
    """A rule schedule from its dict form, ``None`` when absent."""
    if value is None:
        return None
    try:
        return Schedule.from_dict(value)
    except RuleValidationError as exc:
        raise ToolError(f"invalid schedule: {exc}") from None
