"""Plain-English watch rules: the ``RuleSpec`` model, its state machine and the rule compiler.

Three pieces live here:

``RuleSpec``
    The persisted description of one watch rule (question, mode, timing, ROI, schedule,
    action). Validated on construction-by-``from_dict`` and on ``validate()``; round-trips
    through JSON with ``to_json`` / ``from_json``.

``RuleStateMachine``
    Turns a stream of judge verdicts into fire decisions. Pure (no I/O) and clock-injected so
    it is fully deterministic under test. States: ``idle -> candidate -> firing -> cooldown``.

``compile_rule(text, source, llm)``
    One structured LLM call that turns plain English into a ``RuleSpec``, with a deterministic
    fallback when no LLM is available or the call fails.

Rule modes
----------
``trigger``
    Fire once when the visual question becomes true (k-of-n confirmed). ``hold_seconds`` is
    ignored. The rule then latches until the question is confirmed false again.
``condition``
    Fire when the question has been true continuously for ``hold_seconds`` (and k-of-n
    confirmed). Latches like ``trigger``: one alert per continuous episode.
``absence``
    The question asks whether something *expected* is present ("Is the dog on its bed?").
    A confident **negative** verdict counts as evidence; the rule fires when the thing has
    been missing for ``hold_seconds``. Keeping the question positive makes the judge's job
    easier than asking it to confirm a negative.

LLM task choice
---------------
Rule compilation is text-only, so it uses its own auxiliary task ``sentinel_rules``
(``RULES_TASK``) rather than the vision task ``sentinel_vision``. Users can route the
compiler to a cheap text model while the judge uses a vision model. ``register()`` should
register it with ``ctx.register_auxiliary_task(RULES_TASK, **RULES_TASK_INFO)``. If the task
is not registered (or the host refuses it), compilation falls back to the deterministic
parser instead of failing.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "MODES",
    "RULES_TASK",
    "RULES_TASK_INFO",
    "COMPILE_SCHEMA",
    "FireEvent",
    "RuleSpec",
    "RuleState",
    "RuleStateMachine",
    "RuleValidationError",
    "Schedule",
    "StepResult",
    "compile_rule",
    "fallback_rule",
    "new_rule_id",
]

# --------------------------------------------------------------------------- constants

MODE_TRIGGER = "trigger"
MODE_CONDITION = "condition"
MODE_ABSENCE = "absence"
MODES: tuple[str, ...] = (MODE_TRIGGER, MODE_CONDITION, MODE_ABSENCE)

ACTION_NOTIFY = "notify"
ACTION_AGENT = "agent"
ACTIONS: tuple[str, ...] = (ACTION_NOTIFY, ACTION_AGENT)

COMPILED_BY_LLM = "llm"
COMPILED_BY_FALLBACK = "fallback"
COMPILED_BY_MANUAL = "manual"
_COMPILED_BY = (COMPILED_BY_LLM, COMPILED_BY_FALLBACK, COMPILED_BY_MANUAL)

#: Auxiliary LLM task used by :func:`compile_rule` (text-only; see module docstring).
RULES_TASK = "sentinel_rules"
#: Keyword arguments for ``ctx.register_auxiliary_task(RULES_TASK, **RULES_TASK_INFO)``.
RULES_TASK_INFO: dict[str, Any] = {
    "display_name": "Sentinel rule compiler",
    "description": "Turns a plain-English watch request into a structured Sentinel rule "
    "(text only; a small fast model is enough).",
}

MAX_TEXT_LEN = 2000
MAX_QUESTION_LEN = 500
MAX_ACTION_PROMPT_LEN = 2000
MAX_HOLD_SECONDS = 7 * 24 * 3600.0
MAX_COOLDOWN_SECONDS = 7 * 24 * 3600.0
MAX_CONFIRM_N = 20
MAX_ORIGIN_KEYS = 32
MAX_ORIGIN_VALUE_LEN = 512
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_CONFIRM_K = 2
DEFAULT_CONFIRM_N = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_STALE_AFTER_SECONDS = 900.0


class RuleValidationError(ValueError):
    """A rule spec (or one of its fields) is malformed."""


def new_rule_id() -> str:
    """Return a fresh short rule id such as ``r3f9a1c2e``."""
    return "r" + uuid.uuid4().hex[:8]


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleValidationError(f"{name} must be a number, got {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise RuleValidationError(f"{name} must be finite, got {value!r}")
    return out


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise RuleValidationError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise RuleValidationError(f"{name} must be an integer, got {value!r}")
    return value


# --------------------------------------------------------------------------- schedule


def _parse_hhmm(value: Any, name: str, *, allow_24: bool) -> int:
    """``"HH:MM"`` -> minutes since midnight. ``"24:00"`` is allowed for an end time."""
    if not isinstance(value, str):
        raise RuleValidationError(f"{name} must be 'HH:MM', got {value!r}")
    m = _HHMM_RE.match(value.strip())
    if not m:
        raise RuleValidationError(f"{name} must be 'HH:MM', got {value!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if allow_24 and hh == 24 and mm == 0:
        return 24 * 60
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise RuleValidationError(f"{name} is not a valid time: {value!r}")
    return hh * 60 + mm


def _parse_day(value: Any) -> int:
    if isinstance(value, bool):
        raise RuleValidationError(f"invalid weekday {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise RuleValidationError(f"weekday must be 0 (Mon) .. 6 (Sun), got {value!r}")
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _DAY_NAMES:
            return _DAY_NAMES[key]
        if key.isdigit():
            return _parse_day(int(key))
    raise RuleValidationError(f"invalid weekday {value!r}")


@dataclass(frozen=True)
class Schedule:
    """Active window for a rule, evaluated against a caller-supplied (local) datetime.

    ``start``/``end`` are ``"HH:MM"``; ``end`` may be ``"24:00"``. When ``end < start`` the
    window crosses midnight and the part after midnight belongs to the previous day's window
    (``days`` names the day the window *starts*). ``start == end`` means all day. ``days``
    uses 0 = Monday .. 6 = Sunday.
    """

    start: str = "00:00"
    end: str = "24:00"
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    def __post_init__(self) -> None:
        _parse_hhmm(self.start, "schedule.start", allow_24=False)
        _parse_hhmm(self.end, "schedule.end", allow_24=True)
        days = tuple(sorted({_parse_day(d) for d in self.days}))
        if not days:
            raise RuleValidationError("schedule.days must name at least one weekday")
        object.__setattr__(self, "days", days)

    def is_active(self, when: datetime) -> bool:
        """True if ``when`` (naive local or aware) falls inside the window."""
        start = _parse_hhmm(self.start, "schedule.start", allow_24=False)
        end = _parse_hhmm(self.end, "schedule.end", allow_24=True)
        minute = when.hour * 60 + when.minute
        day = when.weekday()
        if start == end or (start == 0 and end == 24 * 60):
            return day in self.days
        if start < end:
            return day in self.days and start <= minute < end
        # Overnight window, e.g. 22:00-06:00.
        return (day in self.days and minute >= start) or (
            (day - 1) % 7 in self.days and minute < end
        )

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "days": list(self.days)}

    @classmethod
    def from_dict(cls, data: Any) -> Schedule:
        if isinstance(data, Schedule):
            return data
        if not isinstance(data, Mapping):
            raise RuleValidationError(f"schedule must be an object, got {type(data).__name__}")
        days = data.get("days")
        if days is None:
            days = (0, 1, 2, 3, 4, 5, 6)
        elif isinstance(days, (str, int)):
            days = (days,)
        elif not isinstance(days, Sequence):
            raise RuleValidationError("schedule.days must be a list")
        return cls(
            start=str(data.get("start") or "00:00").strip(),
            end=str(data.get("end") or "24:00").strip(),
            days=tuple(days),
        )


# --------------------------------------------------------------------------- RuleSpec


def _validate_roi(roi: Any) -> tuple[float, float, float, float] | None:
    """ROI is ``(x, y, w, h)`` as fractions of the frame (0..1)."""
    if roi is None:
        return None
    if isinstance(roi, Mapping):
        try:
            roi = (roi["x"], roi["y"], roi["w"], roi["h"])
        except KeyError as exc:
            raise RuleValidationError(f"roi object is missing {exc.args[0]!r}") from None
    if isinstance(roi, (str, bytes)) or not isinstance(roi, Sequence) or len(roi) != 4:
        raise RuleValidationError(f"roi must be [x, y, w, h] fractions, got {roi!r}")
    x, y, w, h = (_finite(v, "roi") for v in roi)
    eps = 1e-6
    if not (0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0):
        raise RuleValidationError(f"roi out of range: {roi!r}")
    if x + w > 1 + eps or y + h > 1 + eps:
        raise RuleValidationError(f"roi extends past the frame: {roi!r}")
    return (x, y, min(w, 1 - x), min(h, 1 - y))


def _validate_origin(origin: Any) -> dict[str, Any]:
    if origin is None:
        return {}
    if not isinstance(origin, Mapping):
        raise RuleValidationError("origin must be an object")
    if len(origin) > MAX_ORIGIN_KEYS:
        raise RuleValidationError(f"origin has more than {MAX_ORIGIN_KEYS} keys")
    out: dict[str, Any] = {}
    for key, value in origin.items():
        if not isinstance(key, str) or not key:
            raise RuleValidationError(f"origin keys must be non-empty strings, got {key!r}")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise RuleValidationError(f"origin[{key!r}] must be a scalar")
        if isinstance(value, str) and len(value) > MAX_ORIGIN_VALUE_LEN:
            raise RuleValidationError(f"origin[{key!r}] is too long")
        out[key] = value
    return out


@dataclass
class RuleSpec:
    """One watch rule. All times are seconds; ``created_at``/``updated_at`` are ISO-8601 UTC.

    ``origin`` holds where alerts go (session / platform identifiers recorded when the rule
    was created); it is opaque to this module.
    """

    question: str
    source: str
    text: str = ""
    id: str = field(default_factory=new_rule_id)
    mode: str = MODE_TRIGGER
    hold_seconds: float = 0.0
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    roi: tuple[float, float, float, float] | None = None
    schedule: Schedule | None = None
    action: str = ACTION_NOTIFY
    action_prompt: str = ""
    confirm_k: int = DEFAULT_CONFIRM_K
    confirm_n: int = DEFAULT_CONFIRM_N
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    enabled: bool = True
    recipe: str | None = None
    compiled_by: str = COMPILED_BY_MANUAL
    origin: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    # ---- validation -----------------------------------------------------------------

    def validate(self) -> RuleSpec:
        """Check every field, normalising in place (numbers to float, ROI clamp, ...).

        Returns ``self`` so calls can chain. Raises :class:`RuleValidationError`.
        """
        if not isinstance(self.id, str) or not _ID_RE.match(self.id):
            raise RuleValidationError(f"id must match {_ID_RE.pattern}, got {self.id!r}")
        if not isinstance(self.question, str) or not self.question.strip():
            raise RuleValidationError("question must be a non-empty string")
        self.question = self.question.strip()
        if len(self.question) > MAX_QUESTION_LEN:
            raise RuleValidationError(f"question longer than {MAX_QUESTION_LEN} characters")
        if not isinstance(self.source, str) or not self.source.strip():
            raise RuleValidationError("source must be a non-empty string")
        self.source = self.source.strip()
        if not isinstance(self.text, str):
            raise RuleValidationError("text must be a string")
        if len(self.text) > MAX_TEXT_LEN:
            raise RuleValidationError(f"text longer than {MAX_TEXT_LEN} characters")
        if self.mode not in MODES:
            raise RuleValidationError(f"mode must be one of {MODES}, got {self.mode!r}")
        self.hold_seconds = _finite(self.hold_seconds, "hold_seconds")
        if not 0 <= self.hold_seconds <= MAX_HOLD_SECONDS:
            raise RuleValidationError(f"hold_seconds must be 0..{MAX_HOLD_SECONDS:g}")
        self.cooldown_seconds = _finite(self.cooldown_seconds, "cooldown_seconds")
        if not 0 <= self.cooldown_seconds <= MAX_COOLDOWN_SECONDS:
            raise RuleValidationError(f"cooldown_seconds must be 0..{MAX_COOLDOWN_SECONDS:g}")
        self.roi = _validate_roi(self.roi)
        if self.schedule is not None and not isinstance(self.schedule, Schedule):
            self.schedule = Schedule.from_dict(self.schedule)
        if self.action not in ACTIONS:
            raise RuleValidationError(f"action must be one of {ACTIONS}, got {self.action!r}")
        if not isinstance(self.action_prompt, str):
            raise RuleValidationError("action_prompt must be a string")
        if len(self.action_prompt) > MAX_ACTION_PROMPT_LEN:
            raise RuleValidationError("action_prompt is too long")
        if self.action == ACTION_AGENT and not self.action_prompt.strip():
            raise RuleValidationError("action 'agent' needs a non-empty action_prompt")
        self.confirm_k = _int(self.confirm_k, "confirm_k")
        self.confirm_n = _int(self.confirm_n, "confirm_n")
        if not 1 <= self.confirm_n <= MAX_CONFIRM_N:
            raise RuleValidationError(f"confirm_n must be 1..{MAX_CONFIRM_N}")
        if not 1 <= self.confirm_k <= self.confirm_n:
            raise RuleValidationError("confirm_k must be 1..confirm_n")
        self.confidence_threshold = _finite(self.confidence_threshold, "confidence_threshold")
        if not 0 <= self.confidence_threshold <= 1:
            raise RuleValidationError("confidence_threshold must be 0..1")
        if not isinstance(self.enabled, bool):
            raise RuleValidationError("enabled must be a boolean")
        if self.recipe is not None and (not isinstance(self.recipe, str) or not self.recipe):
            raise RuleValidationError("recipe must be a non-empty string or null")
        if self.compiled_by not in _COMPILED_BY:
            raise RuleValidationError(f"compiled_by must be one of {_COMPILED_BY}")
        self.origin = _validate_origin(self.origin)
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RuleValidationError(f"{name} must be an ISO-8601 string")
        return self

    @property
    def effective_hold_seconds(self) -> float:
        """Hold actually applied: ``trigger`` rules ignore ``hold_seconds``."""
        return 0.0 if self.mode == MODE_TRIGGER else float(self.hold_seconds)

    def touch(self) -> None:
        """Bump ``updated_at`` to now."""
        self.updated_at = _utc_now_iso()

    def with_changes(self, **changes: Any) -> RuleSpec:
        """Validated copy with ``changes`` applied and ``updated_at`` bumped."""
        unknown = set(changes) - {f.name for f in fields(self)}
        if unknown:
            raise RuleValidationError(f"unknown rule fields: {sorted(unknown)}")
        if "id" in changes and changes["id"] != self.id:
            raise RuleValidationError("a rule's id cannot be changed")
        out = replace(self, **changes)
        out.origin = dict(out.origin)
        out.touch()
        return out.validate()

    # ---- serialisation --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (ROI as a list, schedule as an object)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "roi" and value is not None:
                value = list(value)
            elif f.name == "schedule" and value is not None:
                value = value.to_dict()
            elif f.name == "origin":
                value = dict(value)
            out[f.name] = value
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuleSpec:
        """Build and validate a spec. Unknown keys are ignored (forward compatibility)."""
        if not isinstance(data, Mapping):
            raise RuleValidationError(f"rule must be an object, got {type(data).__name__}")
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for required in ("question", "source"):
            if required not in kwargs:
                raise RuleValidationError(f"rule is missing {required!r}")
        if kwargs.get("schedule") is not None:
            kwargs["schedule"] = Schedule.from_dict(kwargs["schedule"])
        if kwargs.get("origin") is None:
            kwargs.pop("origin", None)
        if kwargs.get("id") is None:
            kwargs.pop("id", None)
        for ts in ("created_at", "updated_at"):
            if kwargs.get(ts) is None:
                kwargs.pop(ts, None)
        try:
            spec = cls(**kwargs)
        except TypeError as exc:  # pragma: no cover - guarded by the key filter above
            raise RuleValidationError(str(exc)) from exc
        return spec.validate()

    @classmethod
    def from_json(cls, text: str | bytes) -> RuleSpec:
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError(f"rule JSON is malformed: {exc}") from exc
        return cls.from_dict(data)


# --------------------------------------------------------------------------- state machine


class RuleState(StrEnum):
    """Lifecycle of one rule's evidence."""

    IDLE = "idle"  # armed; waiting for a positive verdict
    CANDIDATE = "candidate"  # positive evidence seen; waiting for k-of-n and hold
    FIRING = "firing"  # fired; latched until the question is confirmed false
    COOLDOWN = "cooldown"  # re-armed but inside the cooldown window; cannot fire yet


@dataclass(frozen=True)
class _Obs:
    ts: float
    hit: bool
    confidence: float


@dataclass(frozen=True)
class FireEvent:
    """Emitted once when a rule fires."""

    rule_id: str
    mode: str
    ts: float
    confidence: float
    truth_since: float
    held_seconds: float
    hits: int
    window: int


@dataclass(frozen=True)
class StepResult:
    """Outcome of one :meth:`RuleStateMachine.observe` / :meth:`poll` call."""

    state: RuleState
    previous: RuleState
    counted: bool
    fired: FireEvent | None = None

    @property
    def changed(self) -> bool:
        return self.state is not self.previous


class RuleStateMachine:
    """k-of-n confirmation, hold, latch and cooldown for one rule. Pure and clock-injected.

    * A **hit** is a verdict that counts as evidence for the rule: ``match=True`` with
      ``confidence >= threshold`` (``trigger``/``condition``), or ``match=False`` with
      ``confidence >= threshold`` (``absence``). Every other valid verdict is a **miss**.
      ``match=None`` (judge error) or a non-finite confidence is ignored.
    * The window holds the last ``confirm_n`` counted verdicts. Truth is *confirmed* when it
      holds ``>= k`` hits; it is *refuted* when it holds ``>= n - k + 1`` misses (at that
      point ``k`` hits are impossible in the window).
    * ``idle``: a hit opens a candidate episode (``truth_since`` = time of that hit).
    * ``candidate``: refuted -> ``idle``. Confirmed, held for ``effective_hold_seconds``
      and the latest verdict is a hit -> fire, enter ``firing``, start the cooldown.
    * ``firing``: latched (no repeat alerts for the same episode). Refuted -> re-arm into
      ``cooldown`` (if the cooldown is still running) or ``idle``.
    * ``cooldown``: evidence is still recorded but nothing fires until the cooldown ends.
    * Staleness: if no verdict was counted for ``stale_after_seconds``, the episode is
      dropped (window cleared; ``candidate`` -> ``idle``, ``firing`` re-arms). Continuity is
      only claimed across verdicts at most that far apart.
    * Time never goes backwards: a ``now`` earlier than the last one is clamped.

    Thread-safe: an internal lock serialises all calls.
    """

    def __init__(
        self,
        spec: RuleSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        stale = float(stale_after_seconds)
        if not math.isfinite(stale) or stale <= 0:
            raise ValueError("stale_after_seconds must be a positive number")
        self._clock = clock
        self._stale_after = stale
        self._lock = threading.Lock()
        self._spec = spec
        self._window: deque[_Obs] = deque(maxlen=spec.confirm_n)
        self._state = RuleState.IDLE
        self._truth_since: float | None = None
        self._cooldown_until: float = -math.inf
        self._last_now: float = -math.inf
        self._last_counted: float | None = None
        self._last_fired: float | None = None
        self._fire_count = 0

    # ---- public API -----------------------------------------------------------------

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    @property
    def state(self) -> RuleState:
        with self._lock:
            return self._state

    @property
    def fire_count(self) -> int:
        with self._lock:
            return self._fire_count

    def observe(
        self, match: bool | None, confidence: float, now: float | None = None
    ) -> StepResult:
        """Feed one judge verdict. Returns the resulting state and a :class:`FireEvent` if
        the rule fired on this verdict."""
        with self._lock:
            now = self._tick(now)
            previous = self._state
            self._advance_time(now)
            obs = self._to_obs(match, confidence, now)
            if obs is None:
                return StepResult(self._state, previous, counted=False)
            self._window.append(obs)
            self._last_counted = now
            fired = self._step(now)
            return StepResult(self._state, previous, counted=True, fired=fired)

    def poll(self, now: float | None = None) -> StepResult:
        """Apply time-based transitions (cooldown end, staleness) without a verdict."""
        with self._lock:
            now = self._tick(now)
            previous = self._state
            self._advance_time(now)
            return StepResult(self._state, previous, counted=False)

    def reset(self) -> None:
        """Forget all evidence and cooldown; back to ``idle``."""
        with self._lock:
            self._reset_locked(keep_cooldown=False)

    def update_spec(self, spec: RuleSpec) -> None:
        """Swap in an edited spec. A change of mode, k or n resets the evidence (the running
        cooldown is kept); threshold / hold / cooldown edits apply to future verdicts."""
        with self._lock:
            old = self._spec
            self._spec = spec
            if (old.mode, old.confirm_k, old.confirm_n) != (
                spec.mode, spec.confirm_k, spec.confirm_n
            ):
                self._reset_locked(keep_cooldown=True)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """JSON-safe view of the current state for status displays."""
        with self._lock:
            now = self._peek(now)
            hits = sum(1 for o in self._window if o.hit)
            return {
                "rule_id": self._spec.id,
                "state": self._state.value,
                "window": len(self._window),
                "hits": hits,
                "misses": len(self._window) - hits,
                "truth_seconds": (
                    max(0.0, now - self._truth_since) if self._truth_since is not None else None
                ),
                "cooldown_remaining": max(0.0, self._cooldown_until - now)
                if math.isfinite(self._cooldown_until)
                else 0.0,
                "fire_count": self._fire_count,
                "seconds_since_fire": (
                    max(0.0, now - self._last_fired) if self._last_fired is not None else None
                ),
            }

    # ---- internals (lock held) ------------------------------------------------------

    def _peek(self, now: float | None) -> float:
        value = self._clock() if now is None else float(now)
        if not math.isfinite(value):
            raise ValueError("now must be finite")
        return max(value, self._last_now)

    def _tick(self, now: float | None) -> float:
        value = self._peek(now)
        self._last_now = value
        return value

    def _to_obs(self, match: bool | None, confidence: Any, now: float) -> _Obs | None:
        if match is None or not isinstance(match, bool):
            return None
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        conf = float(confidence)
        if not math.isfinite(conf):
            return None
        conf = min(1.0, max(0.0, conf))
        evidence = (not match) if self._spec.mode == MODE_ABSENCE else match
        hit = evidence and conf >= self._spec.confidence_threshold
        return _Obs(ts=now, hit=hit, confidence=conf)

    def _reset_locked(self, *, keep_cooldown: bool) -> None:
        self._window = deque(maxlen=self._spec.confirm_n)
        self._truth_since = None
        self._last_counted = None
        if not keep_cooldown:
            self._cooldown_until = -math.inf
        self._state = (
            RuleState.COOLDOWN
            if keep_cooldown and self._last_now < self._cooldown_until
            else RuleState.IDLE
        )

    def _rearm(self, now: float) -> None:
        self._window.clear()
        self._truth_since = None
        self._state = RuleState.COOLDOWN if now < self._cooldown_until else RuleState.IDLE

    def _advance_time(self, now: float) -> None:
        if self._last_counted is not None and now - self._last_counted > self._stale_after:
            # Lost track: evidence too old to claim continuity.
            self._last_counted = None
            if self._state in (RuleState.CANDIDATE, RuleState.FIRING):
                self._rearm(now)
            else:
                self._window.clear()
        if self._state is RuleState.COOLDOWN and now >= self._cooldown_until:
            self._state = RuleState.IDLE

    def _counts(self) -> tuple[int, int]:
        hits = sum(1 for o in self._window if o.hit)
        return hits, len(self._window) - hits

    def _step(self, now: float) -> FireEvent | None:
        spec = self._spec
        k, n = spec.confirm_k, spec.confirm_n
        refute_at = n - k + 1

        if self._state is RuleState.COOLDOWN:
            return None

        if self._state is RuleState.IDLE:
            # Drop leading misses so the episode starts at its first hit.
            while self._window and not self._window[0].hit:
                self._window.popleft()
            if not self._window:
                return None
            self._state = RuleState.CANDIDATE
            self._truth_since = self._window[0].ts

        hits, misses = self._counts()

        if self._state is RuleState.FIRING:
            if misses >= refute_at:
                self._rearm(now)
            return None

        # CANDIDATE
        if misses >= refute_at:
            self._window.clear()
            self._truth_since = None
            self._state = RuleState.IDLE
            return None
        assert self._truth_since is not None
        held = now - self._truth_since
        if (
            hits >= k
            and self._window[-1].hit  # never fire on a verdict that just said "no"
            and held >= spec.effective_hold_seconds
            and now >= self._cooldown_until
        ):
            hit_confs = [o.confidence for o in self._window if o.hit]
            event = FireEvent(
                rule_id=spec.id,
                mode=spec.mode,
                ts=now,
                confidence=sum(hit_confs) / len(hit_confs),
                truth_since=self._truth_since,
                held_seconds=held,
                hits=hits,
                window=len(self._window),
            )
            self._state = RuleState.FIRING
            self._cooldown_until = now + spec.cooldown_seconds
            self._last_fired = now
            self._fire_count += 1
            return event
        return None


# --------------------------------------------------------------------------- compiler

COMPILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "mode": {"type": "string", "enum": list(MODES)},
        "hold_seconds": {"type": ["number", "null"]},
        "cooldown_seconds": {"type": ["number", "null"]},
        "roi": {
            "anyOf": [
                {"type": "null"},
                {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            ]
        },
        "schedule": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "days": {"type": ["array", "null"]},
                    },
                },
            ]
        },
        "action": {"type": "string", "enum": list(ACTIONS)},
        "action_prompt": {"type": ["string", "null"]},
    },
    "required": ["question", "mode"],
}

_COMPILE_INSTRUCTIONS = """\
You convert a user's plain-English request to watch a camera or screen into a JSON rule.
A vision model will later be shown single frames and asked `question`, so:
- `question` is ONE short yes/no question answerable from a single still image,
  e.g. "Is there a package on the doorstep?". No time words in the question.
- `mode`:
  * "trigger": alert when the answer becomes yes ("tell me if someone takes my mug" ->
    question "Is the coffee mug missing from the desk?", mode trigger).
  * "condition": alert when the answer stays yes for hold_seconds ("stove on and nobody in
    the kitchen for 5 minutes" -> hold_seconds 300).
  * "absence": something EXPECTED is missing for hold_seconds. Phrase the question
    POSITIVELY about the expected thing ("dog not on its bed for 10 min" -> question
    "Is the dog on its bed?", mode absence, hold_seconds 600).
- `hold_seconds`: required duration for condition/absence, else 0.
- `cooldown_seconds`: minimum gap between alerts if the user states one, else null.
- `roi`: [x, y, w, h] as fractions 0..1 only if the user names a clear region of the frame
  ("top-left corner", "left half"), else null.
- `schedule`: {"start": "HH:MM", "end": "HH:MM", "days": [0..6 with 0=Monday] or null} only
  if the user limits when to watch ("at night" -> 22:00-06:00), else null.
- `action`: "agent" only if the user wants something DONE beyond being told (then put the
  instruction in `action_prompt`), else "notify" with action_prompt null.
Return only the JSON object."""

_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:hey|ok|okay)\s+\w+[,\s]+)?"
    r"(?:(?:can|could|would)\s+you\s+)?"
    r"(?:(?:tell|alert|notify|ping|text|message|warn|wake)\s+me\s+(?:when(?:ever)?|if|once)"
    r"|let\s+me\s+know\s+(?:when(?:ever)?|if|once)"
    r"|watch\s+(?:for|if|whether|when)"
    r"|look\s+(?:out\s+)?for"
    r"|check\s+(?:if|whether)"
    r"|(?:when(?:ever)?|if))\s+",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\b(?:for|over|longer\s+than|more\s+than|at\s+least)\s+"
    r"(\d+(?:\.\d+)?|an?|one)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def _duration_seconds(amount: str, unit: str) -> float:
    amount = amount.lower()
    value = 1.0 if amount in ("a", "an", "one") else float(amount)
    return value * _UNIT_SECONDS[unit.lower()[0]]


def _clean_text(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError("rule text must be a string")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("rule text is empty")
    return cleaned[:MAX_TEXT_LEN]


def _rule_defaults(defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pick the config-driven defaults that apply to a new rule; bad values are dropped."""
    out: dict[str, Any] = {}
    if not defaults:
        return out
    for key in ("confirm_k", "confirm_n", "confidence_threshold", "cooldown_seconds"):
        if key in defaults and defaults[key] is not None:
            out[key] = defaults[key]
    try:
        probe = RuleSpec(question="probe", source="probe", **out)
        probe.validate()
    except (RuleValidationError, TypeError):
        logger.warning("sentinel: ignoring invalid rule defaults %r", out)
        return {}
    return {k: getattr(probe, k) for k in out}


def fallback_rule(
    text: str,
    source: str,
    *,
    defaults: Mapping[str, Any] | None = None,
    origin: Mapping[str, Any] | None = None,
) -> RuleSpec:
    """Deterministic, offline rule from plain text.

    The question is the request with the leading "tell me if/when ..." stripped. A duration
    phrase ("for 5 minutes") turns the rule into a ``condition`` with that hold.
    """
    cleaned = _clean_text(text)
    body = _PREFIX_RE.sub("", cleaned, count=1).strip() or cleaned
    mode = MODE_TRIGGER
    hold = 0.0
    m = _DURATION_RE.search(body)
    if m:
        hold = min(MAX_HOLD_SECONDS, _duration_seconds(m.group(1), m.group(2)))
        if hold > 0:
            mode = MODE_CONDITION
            stripped = (body[: m.start()] + body[m.end():]).strip(" ,.;")
            body = " ".join(stripped.split()) or body
    body = body.rstrip(" ?.!")
    question = f"Does the image show this: {body}?"
    if len(question) > MAX_QUESTION_LEN:
        question = question[: MAX_QUESTION_LEN - 1].rstrip() + "?"
    spec = RuleSpec(
        question=question,
        source=source,
        text=cleaned,
        mode=mode,
        hold_seconds=hold,
        compiled_by=COMPILED_BY_FALLBACK,
        origin=dict(origin or {}),
        **_rule_defaults(defaults),
    )
    return spec.validate()


def _coerce_llm_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Salvage what we can from the model's output; invalid optional fields are dropped."""
    out: dict[str, Any] = {}
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise RuleValidationError("model returned no question")
    question = " ".join(question.split())
    if len(question) > MAX_QUESTION_LEN:
        question = question[: MAX_QUESTION_LEN - 1].rstrip() + "?"
    out["question"] = question
    mode = data.get("mode")
    out["mode"] = mode if mode in MODES else MODE_TRIGGER

    def number(key: str, upper: float) -> float | None:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        return min(value, upper)

    hold = number("hold_seconds", MAX_HOLD_SECONDS)
    if hold is not None:
        out["hold_seconds"] = hold
    cooldown = number("cooldown_seconds", MAX_COOLDOWN_SECONDS)
    if cooldown is not None:
        out["cooldown_seconds"] = cooldown
    if out["mode"] == MODE_TRIGGER and out.get("hold_seconds", 0) > 0:
        # A trigger with a duration is really a condition.
        out["mode"] = MODE_CONDITION
    try:
        out["roi"] = _validate_roi(data.get("roi"))
    except RuleValidationError:
        logger.info("sentinel: dropping invalid ROI from compiled rule: %r", data.get("roi"))
    if data.get("schedule") is not None:
        try:
            out["schedule"] = Schedule.from_dict(data["schedule"])
        except RuleValidationError:
            logger.info("sentinel: dropping invalid schedule: %r", data.get("schedule"))
    prompt = data.get("action_prompt")
    prompt = " ".join(prompt.split())[:MAX_ACTION_PROMPT_LEN] if isinstance(prompt, str) else ""
    if data.get("action") == ACTION_AGENT and prompt:
        out["action"] = ACTION_AGENT
        out["action_prompt"] = prompt
    return out


def compile_rule(
    text: str,
    source: str,
    llm: Any | None,
    *,
    defaults: Mapping[str, Any] | None = None,
    origin: Mapping[str, Any] | None = None,
    task: str | None = RULES_TASK,
    timeout: float | None = 30.0,
) -> RuleSpec:
    """Turn a plain-English request into a validated :class:`RuleSpec`.

    Makes one ``llm.complete_structured`` call (``task=RULES_TASK``,
    ``purpose="sentinel:compile"``). Any failure - no LLM, trust refusal, timeout, schema
    mismatch, unusable output - falls back to :func:`fallback_rule`; this function only
    raises ``ValueError`` for empty/non-string ``text`` or ``source``. Check
    ``spec.compiled_by`` (``"llm"`` / ``"fallback"``) to see which path was taken.

    ``defaults`` may carry ``confirm_k``, ``confirm_n``, ``confidence_threshold`` and
    ``cooldown_seconds`` from plugin config; an explicit cooldown in the request wins.
    """
    cleaned = _clean_text(text)
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if llm is None:
        return fallback_rule(cleaned, source, defaults=defaults, origin=origin)
    try:
        result = llm.complete_structured(
            instructions=_COMPILE_INSTRUCTIONS,
            input=[{"type": "text", "text": f"Request: {cleaned}\nSource: {source.strip()}"}],
            json_schema=COMPILE_SCHEMA,
            schema_name="sentinel_rule",
            temperature=0.0,
            max_tokens=400,
            timeout=timeout,
            purpose="sentinel:compile",
            task=task,
        )
        parsed = getattr(result, "parsed", None)
        if not isinstance(parsed, Mapping):
            raise RuleValidationError("model returned no JSON object")
        fields_ = _coerce_llm_fields(parsed)
        base = _rule_defaults(defaults)
        base.update(fields_)
        spec = RuleSpec(
            source=source,
            text=cleaned,
            compiled_by=COMPILED_BY_LLM,
            origin=dict(origin or {}),
            **base,
        )
        return spec.validate()
    except Exception as exc:  # noqa: BLE001 - any LLM failure degrades to the fallback
        logger.warning("sentinel: rule compiler fell back to the offline parser: %s", exc)
        return fallback_rule(cleaned, source, defaults=defaults, origin=origin)
