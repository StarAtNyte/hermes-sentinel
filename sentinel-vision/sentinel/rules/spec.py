"""The persisted description of one watch rule."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any

from .constants import (
    ACTION_NOTIFY,
    COMPILED_BY_MANUAL,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_CONFIRM_K,
    DEFAULT_CONFIRM_N,
    DEFAULT_COOLDOWN_SECONDS,
    MODE_TRIGGER,
)
from .errors import RuleValidationError
from .schedule import Schedule
from .spec_validation import (
    validate_action,
    validate_confirmation,
    validate_metadata,
    validate_text_fields,
    validate_timing,
)
from .validation import new_rule_id, utc_now_iso

_OPTIONAL_DEFAULTED = ("origin", "id", "created_at", "updated_at")


def _serialise_field(name: str, value: Any) -> Any:
    """JSON-safe form of one spec field."""
    if name == "roi" and value is not None:
        return list(value)
    if name == "schedule" and value is not None:
        return value.to_dict()
    if name == "origin":
        return dict(value)
    return value


@dataclass
class RuleSpec:
    """One watch rule. All times are seconds; ``created_at``/``updated_at`` are ISO-8601 UTC.

    ``motion`` marks a question about movement or change over time; the judge then sees a
    short ordered sequence of recent frames instead of one still.

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
    motion: bool = False
    compiled_by: str = COMPILED_BY_MANUAL
    origin: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def validate(self) -> RuleSpec:
        """Check every field, normalising in place (numbers to float, ROI clamp, ...).

        Returns ``self`` so calls can chain. Raises :class:`RuleValidationError`.
        """
        validate_text_fields(self)
        validate_timing(self)
        validate_action(self)
        validate_confirmation(self)
        validate_metadata(self)
        return self

    @property
    def effective_hold_seconds(self) -> float:
        """Hold actually applied: ``trigger`` rules ignore ``hold_seconds``."""
        return 0.0 if self.mode == MODE_TRIGGER else float(self.hold_seconds)

    def touch(self) -> None:
        """Bump ``updated_at`` to now."""
        self.updated_at = utc_now_iso()

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

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (ROI as a list, schedule as an object)."""
        return {f.name: _serialise_field(f.name, getattr(self, f.name)) for f in fields(self)}

    def to_json(self) -> str:
        """Compact, key-sorted JSON of :meth:`to_dict`."""
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
        for name in _OPTIONAL_DEFAULTED:
            if kwargs.get(name) is None:
                kwargs.pop(name, None)
        try:
            spec = cls(**kwargs)
        except TypeError as exc:  # pragma: no cover - guarded by the key filter above
            raise RuleValidationError(str(exc)) from exc
        return spec.validate()

    @classmethod
    def from_json(cls, text: str | bytes) -> RuleSpec:
        """Parse :meth:`to_json` output (or any equivalent JSON object)."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RuleValidationError(f"rule JSON is malformed: {exc}") from exc
        return cls.from_dict(data)
