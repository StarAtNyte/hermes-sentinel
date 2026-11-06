"""Plugin settings: the ``config_schema`` keys of ``plugin.yaml``, read defensively.

Every key is read through ``ctx.get_config`` (plugin-relative keys under
``plugins.entries.hermes-sentinel.settings``). Bad values never raise: they fall back to the
default and the problem is recorded in :attr:`SentinelConfig.problems` so ``/sentinel status``
can show it.

``CONFIG_KEYS`` is the single source of truth for which keys exist; ``test_config.py`` checks it
against ``plugin.yaml``'s ``config_schema``.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .budget import DEFAULT_PER_DAY, DEFAULT_PER_HOUR, MAX_CAP

logger = logging.getLogger(__name__)

__all__ = ["CONFIG_KEYS", "DEFAULT_SOURCE", "SentinelConfig"]

DEFAULT_SOURCE = "camera:0"
MAX_STREAMS = 64
MAX_STREAM_NAME_LEN = 64
MAX_STREAM_URL_LEN = 2048
_STREAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: Every settings key the plugin reads (must equal ``config_schema`` in plugin.yaml).
CONFIG_KEYS: tuple[str, ...] = (
    "default_source",
    "vision_calls_per_hour",
    "vision_calls_per_day",
    "confirm_k",
    "confirm_n",
    "confidence_threshold",
    "idle_check_seconds",
    "retention_days",
    "max_storage_mb",
    "stream_urls",
    "alert_snapshots",
)


def _as_int(value: Any, default: int, lo: int, hi: int, name: str, problems: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        problems.append(f"{name}: expected an integer, got {type(value).__name__}")
        return default
    try:
        number = float(value)
    except ValueError:
        problems.append(f"{name}: {value!r} is not a number")
        return default
    if not math.isfinite(number) or number != int(number):
        problems.append(f"{name}: {value!r} is not a whole number")
        return default
    out = int(number)
    if not lo <= out <= hi:
        problems.append(f"{name}: {out} is outside {lo}..{hi}")
        return default
    return out


def _as_float(
    value: Any, default: float, lo: float, hi: float, name: str, problems: list[str]
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        problems.append(f"{name}: expected a number, got {type(value).__name__}")
        return default
    try:
        number = float(value)
    except ValueError:
        problems.append(f"{name}: {value!r} is not a number")
        return default
    if not math.isfinite(number) or not lo <= number <= hi:
        problems.append(f"{name}: {value!r} is outside {lo:g}..{hi:g}")
        return default
    return number


def _as_bool(value: Any, default: bool, name: str, problems: list[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    problems.append(f"{name}: {value!r} is not a boolean")
    return default


def _as_cap(value: Any, default: int, name: str, problems: list[str]) -> int | None:
    """A vision-call cap: non-negative int; ``null``/``"unlimited"``/-1 mean no cap."""
    if value is None:
        return default
    if isinstance(value, str) and value.strip().lower() in ("unlimited", "none", "off"):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == -1:
        return None
    return _as_int(value, default, 0, MAX_CAP, name, problems)


def _as_streams(value: Any, problems: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        problems.append("stream_urls: expected a mapping of name -> URL")
        return {}
    out: dict[str, str] = {}
    for raw_name, raw_url in value.items():
        if len(out) >= MAX_STREAMS:
            problems.append(f"stream_urls: more than {MAX_STREAMS} entries; extra ignored")
            break
        name = str(raw_name).strip()
        if not name or len(name) > MAX_STREAM_NAME_LEN or not _STREAM_NAME_RE.match(name):
            problems.append(f"stream_urls: invalid stream name {str(raw_name)[:40]!r}")
            continue
        if not isinstance(raw_url, str) or not raw_url.strip():
            problems.append(f"stream_urls.{name}: URL must be a non-empty string")
            continue
        url = raw_url.strip()
        if len(url) > MAX_STREAM_URL_LEN:
            problems.append(f"stream_urls.{name}: URL is too long")
            continue
        out[name] = url
    return out


@dataclass(frozen=True)
class SentinelConfig:
    """Validated plugin settings (defaults match ``plugin.yaml``)."""

    default_source: str = DEFAULT_SOURCE
    vision_calls_per_hour: int | None = DEFAULT_PER_HOUR
    vision_calls_per_day: int | None = DEFAULT_PER_DAY
    confirm_k: int = 2
    confirm_n: int = 3
    confidence_threshold: float = 0.7
    idle_check_seconds: float = 300.0
    retention_days: float = 7.0
    max_storage_mb: float = 500.0
    stream_urls: dict[str, str] = field(default_factory=dict)
    alert_snapshots: bool = True
    problems: tuple[str, ...] = ()

    @classmethod
    def load(cls, get: Callable[[str, Any], Any] | None) -> SentinelConfig:
        """Read every key via ``get(key, default)`` (``ctx.get_config``). Never raises."""
        problems: list[str] = []

        def read(key: str) -> Any:
            if get is None:
                return None
            try:
                return get(key, None)
            except Exception as exc:  # noqa: BLE001 - a broken host config must not kill load
                problems.append(f"{key}: could not be read ({type(exc).__name__})")
                return None

        raw = {key: read(key) for key in CONFIG_KEYS}
        source = raw["default_source"]
        if source is None:
            default_source = DEFAULT_SOURCE
        elif (
            isinstance(source, (str, int))
            and not isinstance(source, bool)
            and str(source).strip()
        ):
            default_source = str(source).strip()
        else:
            problems.append("default_source: expected a source string like 'camera:0'")
            default_source = DEFAULT_SOURCE
        k = _as_int(raw["confirm_k"], 2, 1, 20, "confirm_k", problems)
        n = _as_int(raw["confirm_n"], 3, 1, 20, "confirm_n", problems)
        if k > n:
            problems.append(f"confirm_k ({k}) is larger than confirm_n ({n}); using 2 of 3")
            k, n = 2, 3
        cfg = cls(
            default_source=default_source,
            vision_calls_per_hour=_as_cap(
                raw["vision_calls_per_hour"], DEFAULT_PER_HOUR, "vision_calls_per_hour", problems
            ),
            vision_calls_per_day=_as_cap(
                raw["vision_calls_per_day"], DEFAULT_PER_DAY, "vision_calls_per_day", problems
            ),
            confirm_k=k,
            confirm_n=n,
            confidence_threshold=_as_float(
                raw["confidence_threshold"], 0.7, 0.0, 1.0, "confidence_threshold", problems
            ),
            idle_check_seconds=_as_float(
                raw["idle_check_seconds"], 300.0, 0.0, 7 * 86400.0, "idle_check_seconds", problems
            ),
            retention_days=_as_float(
                raw["retention_days"], 7.0, 0.01, 3650.0, "retention_days", problems
            ),
            max_storage_mb=_as_float(
                raw["max_storage_mb"], 500.0, 1.0, 1_000_000.0, "max_storage_mb", problems
            ),
            stream_urls=_as_streams(raw["stream_urls"], problems),
            alert_snapshots=_as_bool(raw["alert_snapshots"], True, "alert_snapshots", problems),
            problems=tuple(problems),
        )
        for problem in problems:
            logger.warning("sentinel config: %s", problem)
        return cfg

    def rule_defaults(self) -> dict[str, Any]:
        """Defaults applied to newly created rules."""
        return {
            "confirm_k": self.confirm_k,
            "confirm_n": self.confirm_n,
            "confidence_threshold": self.confidence_threshold,
        }

    @property
    def stale_after_seconds(self) -> float:
        """How long a rule keeps evidence without a verdict (about three idle checks)."""
        idle = self.idle_check_seconds if self.idle_check_seconds > 0 else 300.0
        return max(900.0, 3.0 * idle)
