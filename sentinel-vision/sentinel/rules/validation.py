"""Field-level validators and small helpers shared by the rule model and the compiler."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .constants import (
    DAY_NAMES,
    HHMM_RE,
    MAX_ORIGIN_KEYS,
    MAX_ORIGIN_VALUE_LEN,
)
from .errors import RuleValidationError

Roi = tuple[float, float, float, float]


def new_rule_id() -> str:
    """Return a fresh short rule id such as ``r3f9a1c2e``."""
    return "r" + uuid.uuid4().hex[:8]


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def finite(value: Any, name: str) -> float:
    """Coerce a real number (not a bool) to a finite float or raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleValidationError(f"{name} must be a number, got {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise RuleValidationError(f"{name} must be finite, got {value!r}")
    return out


def to_int(value: Any, name: str) -> int:
    """Coerce an integer (or integral float, not a bool) or raise."""
    if isinstance(value, bool):
        raise RuleValidationError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise RuleValidationError(f"{name} must be an integer, got {value!r}")
    return value


def parse_hhmm(value: Any, name: str, *, allow_24: bool) -> int:
    """``"HH:MM"`` -> minutes since midnight. ``"24:00"`` is allowed for an end time."""
    if not isinstance(value, str):
        raise RuleValidationError(f"{name} must be 'HH:MM', got {value!r}")
    match = HHMM_RE.match(value.strip())
    if not match:
        raise RuleValidationError(f"{name} must be 'HH:MM', got {value!r}")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if allow_24 and hours == 24 and minutes == 0:
        return 24 * 60
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise RuleValidationError(f"{name} is not a valid time: {value!r}")
    return hours * 60 + minutes


def parse_day(value: Any) -> int:
    """Weekday number (0 = Monday) from an int, a name or a digit string."""
    if isinstance(value, bool):
        raise RuleValidationError(f"invalid weekday {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise RuleValidationError(f"weekday must be 0 (Mon) .. 6 (Sun), got {value!r}")
    if isinstance(value, str):
        key = value.strip().lower()
        if key in DAY_NAMES:
            return DAY_NAMES[key]
        if key.isdigit():
            return parse_day(int(key))
    raise RuleValidationError(f"invalid weekday {value!r}")


def _roi_sequence(roi: Any) -> Sequence[Any]:
    if isinstance(roi, Mapping):
        try:
            roi = (roi["x"], roi["y"], roi["w"], roi["h"])
        except KeyError as exc:
            raise RuleValidationError(f"roi object is missing {exc.args[0]!r}") from None
    if isinstance(roi, (str, bytes)) or not isinstance(roi, Sequence) or len(roi) != 4:
        raise RuleValidationError(f"roi must be [x, y, w, h] fractions, got {roi!r}")
    return roi


def validate_roi(roi: Any) -> Roi | None:
    """ROI is ``(x, y, w, h)`` as fractions of the frame (0..1)."""
    if roi is None:
        return None
    roi = _roi_sequence(roi)
    x, y, w, h = (finite(v, "roi") for v in roi)
    eps = 1e-6
    if not (0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0):
        raise RuleValidationError(f"roi out of range: {roi!r}")
    if x + w > 1 + eps or y + h > 1 + eps:
        raise RuleValidationError(f"roi extends past the frame: {roi!r}")
    return (x, y, min(w, 1 - x), min(h, 1 - y))


def _check_origin_entry(key: Any, value: Any) -> None:
    if not isinstance(key, str) or not key:
        raise RuleValidationError(f"origin keys must be non-empty strings, got {key!r}")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise RuleValidationError(f"origin[{key!r}] must be a scalar")
    if isinstance(value, str) and len(value) > MAX_ORIGIN_VALUE_LEN:
        raise RuleValidationError(f"origin[{key!r}] is too long")


def validate_origin(origin: Any) -> dict[str, Any]:
    """Alert-routing origin: a small mapping of scalar values."""
    if origin is None:
        return {}
    if not isinstance(origin, Mapping):
        raise RuleValidationError("origin must be an object")
    if len(origin) > MAX_ORIGIN_KEYS:
        raise RuleValidationError(f"origin has more than {MAX_ORIGIN_KEYS} keys")
    out: dict[str, Any] = {}
    for key, value in origin.items():
        _check_origin_entry(key, value)
        out[key] = value
    return out
