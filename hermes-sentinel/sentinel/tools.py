"""Agent tools (DESIGN.md section 3).

Every handler has the Hermes signature ``handler(args: dict, **kwargs) -> str`` and returns a
JSON object: ``{"ok": true, ...}`` on success or ``{"ok": false, "error": "<friendly text>"}``.
Handlers never raise; unexpected failures are logged and reported as a short error.

``kwargs`` carries the calling session (``session_id``, ``task_id``, ...); ``sentinel_watch``
stores it (plus the session's routing key) on the rule so alerts reach the same chat later.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .alerts import (
    GATEWAY_INJECTION_YAML,
    capture_origin,
    format_delivery_test,
    is_chat_origin,
    route_hint,
    run_delivery_test,
    visible_to,
)
from .config import SentinelConfig
from .rules import (
    ACTION_AGENT,
    ACTION_NOTIFY,
    MODES,
    RECIPES,
    RuleSpec,
    RuleValidationError,
    Schedule,
    build_rule,
    compile_rule,
    get_recipe,
    lowered_threshold,
    raised_threshold,
    recipe_names,
)
from .runtime import Sentinel
from .setup_check import SetupEnv, fail_titles, format_report, run_checks, summarize
from .sources import SourceError, SourceSpec, SourceSpecError, list_sources
from .storage import EventRecord, StoreError
from .vision import encode_jpeg

logger = logging.getLogger(__name__)

__all__ = ["TOOLSET", "TOOL_NAMES", "SentinelTools", "parse_time_bound", "register_tools"]

TOOLSET = "sentinel"
MAX_RULES = 50
MAX_TEXT = 2000
MAX_IMAGE_FILE_BYTES = 20 * 1024 * 1024
#: Teaching examples are re-encoded to this longest side (they are re-sent with every check).
EXAMPLE_MAX_SIDE = 768
EXAMPLE_JPEG_QUALITY = 80
#: Plain-English rule compiling costs an LLM call outside the vision budget: small own cap.
COMPILE_CALLS_PER_HOUR = 10
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
_REL_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|"
    r"week|weeks)\s*(?:ago)?\s*$",
    re.IGNORECASE,
)
_REL_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "week": 604800,
    "weeks": 604800,
}

# --------------------------------------------------------------------------- schemas


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_ROI_PROP = {
    "type": "array",
    "items": {"type": "number", "minimum": 0, "maximum": 1},
    "minItems": 4,
    "maxItems": 4,
    "description": "Region of interest as fractions of the frame: [x, y, width, height], "
    "e.g. [0.5, 0.5, 0.5, 0.5] is the bottom-right quarter. Only changes inside it count.",
}
_SOURCE_PROP = {
    "type": "string",
    "description": "Where to look: 'camera:0' (webcam index), 'screen' or 'screen:1' "
    "(monitor), 'screen:1@x,y,w,h' (screen region), 'rtsp://...' or 'stream:<name>' (a "
    "configured stream), 'snapshot:https://...' (JPEG URL), or 'folder:/path' (newest image). "
    "Defaults to the configured default_source. Use sentinel_sources to list options.",
}
_SCHEDULE_PROP = {
    "type": "object",
    "description": "Optional active hours (local time); outside them the rule is idle.",
    "properties": {
        "start": {"type": "string", "description": "HH:MM, e.g. 22:00"},
        "end": {"type": "string", "description": "HH:MM, e.g. 06:00 (may cross midnight)"},
        "days": {
            "type": "array",
            "items": {"type": ["integer", "string"]},
            "description": "Weekdays: 0=Mon..6=Sun or names like 'mon'.",
        },
    },
    "additionalProperties": False,
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "sentinel_sources": _schema(
        "sentinel_sources",
        "List the cameras, monitors and configured streams Sentinel can watch. Probing local "
        "cameras takes a few seconds; set probe_cameras=false to skip it.",
        {"probe_cameras": {"type": "boolean", "description": "Probe webcams 0-4 (default true)."}},
    ),
    "sentinel_watch": _schema(
        "sentinel_watch",
        "Start watching a camera or the screen for something described in plain English, e.g. "
        "'tell me if someone takes my coffee mug'. Sentinel checks frames locally and only asks "
        "the vision model when something changes, then alerts in this chat (with a photo) when "
        "it happens. Use a recipe for common cases: " + ", ".join(sorted(RECIPES)) + ".",
        {
            "what": {"type": "string", "description": "What to watch for, in plain English."},
            "source": _SOURCE_PROP,
            "recipe": {
                "type": "string",
                "enum": sorted(RECIPES),
                "description": "Use a bundled, tuned rule instead of compiling 'what'.",
            },
            "roi": _ROI_PROP,
            "hold_seconds": {
                "type": "number",
                "minimum": 0,
                "description": "Require the condition to hold this long first.",
            },
            "cooldown_seconds": {
                "type": "number",
                "minimum": 0,
                "description": "Quiet time after an alert (default 300).",
            },
            "schedule": _SCHEDULE_PROP,
            "motion": {
                "type": "boolean",
                "description": "True when the question is about movement over time (someone "
                "walking toward the door, a level rising): the vision model then sees a short "
                "ordered sequence of recent frames. Auto-detected when omitted.",
            },
            "action_prompt": {
                "type": "string",
                "description": "Optional instruction for you (the agent) to carry "
                "out when it fires, e.g. 'turn on the porch light'.",
            },
        },
    ),
    "sentinel_rules": _schema(
        "sentinel_rules",
        "List watch rules with their status (watching, paused, source offline), last event and "
        "vision checks used. A 'hint' on a rule means its alerts do not know this chat yet "
        "(tell the user to send /sentinel here).",
        {"rule_id": {"type": "string", "description": "Show only this rule."}},
    ),
    "sentinel_update": _schema(
        "sentinel_update",
        "Pause, resume, delete or edit a watch rule.",
        {
            "rule_id": {"type": "string", "description": "The rule id (see sentinel_rules)."},
            "action": {"type": "string", "enum": ["pause", "resume", "delete", "edit"]},
            "changes": {
                "type": "object",
                "description": "For action=edit: fields to change.",
                "properties": {
                    "question": {"type": "string", "description": "New yes/no visual question."},
                    "mode": {"type": "string", "enum": list(MODES)},
                    "hold_seconds": {"type": "number", "minimum": 0},
                    "cooldown_seconds": {"type": "number", "minimum": 0},
                    "confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "roi": {**_ROI_PROP, "type": ["array", "null"]},
                    "source": {"type": "string"},
                    "action_prompt": {
                        "type": "string",
                        "description": "Empty string = just notify.",
                    },
                    "schedule": {**_SCHEDULE_PROP, "type": ["object", "null"]},
                    "motion": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            "purge_events": {
                "type": "boolean",
                "description": "With delete: also delete its events and photos.",
            },
        },
        ["rule_id", "action"],
    ),
    "sentinel_look": _schema(
        "sentinel_look",
        "Grab a frame from a camera or the screen right now and describe it, or answer a "
        "question about it. Returns a MEDIA: path to the photo. Uses one vision check.",
        {
            "source": _SOURCE_PROP,
            "question": {"type": "string", "description": "Optional question about the frame."},
        },
    ),
    "sentinel_events": _schema(
        "sentinel_events",
        "Search what Sentinel saw: confirmed events with captions, times and photos. Combine "
        "free text with a time range, e.g. text='door', since='today'.",
        {
            "text": {"type": "string", "description": "Words to search captions for."},
            "rule_id": {"type": "string"},
            "since": {
                "type": "string",
                "description": "ISO-8601 time, 'today', 'yesterday', or '2h' / '3d' ago.",
            },
            "until": {"type": "string", "description": "Same formats as since (exclusive)."},
            "feedback": {"type": "string", "enum": ["correct", "false_alarm"]},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max events (default 20).",
            },
        },
    ),
    "sentinel_feedback": _schema(
        "sentinel_feedback",
        "Mark an alert as correct or a false alarm. A false alarm becomes a negative example "
        "and makes the rule slightly stricter; a correct one becomes a positive example.",
        {
            "event_id": {"type": "integer", "description": "Event number from the alert."},
            "verdict": {"type": "string", "enum": ["correct", "false_alarm"]},
            "note": {"type": "string", "description": "Optional note, e.g. what it really was."},
        },
        ["event_id", "verdict"],
    ),
    "sentinel_teach": _schema(
        "sentinel_teach",
        "Teach a rule with an example image: kind 'yes' shows what should trigger it, 'no' shows "
        "what should not. Give image_path, event_id, or from_source=true (current frame).",
        {
            "rule_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["yes", "no"]},
            "image_path": {"type": "string", "description": "Path to a JPEG/PNG image file."},
            "event_id": {"type": "integer", "description": "Use this event's photo."},
            "from_source": {
                "type": "boolean",
                "description": "Use the rule's source's current frame.",
            },
            "note": {"type": "string"},
        },
        ["rule_id", "kind"],
    ),
}
SCHEMAS["sentinel_setup_check"] = _schema(
    "sentinel_setup_check",
    "Check whether Sentinel is ready to watch and alert: Python packages, cameras and screens, "
    "default source, storage, alert delivery permission, vision model, caps and rules. Returns "
    "an ordered list of checks (ok / warn / fail, with the exact fix) and a ready flag. Use it "
    "when the user is setting up, or when watching or alerts do not work.",
    {
        "test_delivery": {
            "type": "boolean",
            "description": "Also send one small test message to this chat and report whether "
            "Hermes accepted it (starts one agent turn; only when the user asks to test "
            "alert delivery).",
        }
    },
)
TOOL_NAMES: tuple[str, ...] = tuple(SCHEMAS)
EMOJI = {
    "sentinel_sources": "\U0001f4f7",
    "sentinel_watch": "\U0001f441",
    "sentinel_rules": "\U0001f4cb",
    "sentinel_update": "\u270f",
    "sentinel_look": "\U0001f50d",
    "sentinel_events": "\U0001f5c2",
    "sentinel_feedback": "\U0001f44d",
    "sentinel_teach": "\U0001f393",
    "sentinel_setup_check": "\U0001fa7a",
}


# --------------------------------------------------------------------------- helpers


class ToolError(Exception):
    """A user-facing error (its message is returned as ``error``)."""


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, default=str, ensure_ascii=False)


def _err(message: str, **data: Any) -> str:
    return json.dumps({"ok": False, "error": message, **data}, default=str, ensure_ascii=False)


def _str_arg(
    args: Mapping[str, Any], key: str, *, required: bool = False, limit: int = MAX_TEXT
) -> str:
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


def _int_arg(args: Mapping[str, Any], key: str, *, required: bool = False) -> int | None:
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


def _num_arg(args: Mapping[str, Any], key: str) -> float | None:
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


def _bool_arg(args: Mapping[str, Any], key: str) -> bool:
    value = args.get(key)
    if not isinstance(value, bool):
        raise ToolError(f"'{key}' must be true or false")
    return value


def _local(ts: str) -> str:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return str(ts)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_time_bound(value: Any, now: datetime | None = None) -> datetime | None:
    """``None``, ISO-8601, ``today``/``yesterday``/``now`` or a relative ``2h`` / ``3 days ago``.

    Naive ISO times are local. Returns an aware datetime. Raises :class:`ToolError`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ToolError("time bounds must be strings")
    now = now or datetime.now().astimezone()
    text = value.strip().lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "now":
        return now
    if text in ("today", "this morning"):
        return midnight
    if text == "yesterday":
        return midnight - timedelta(days=1)
    if text in ("last hour", "past hour"):
        return now - timedelta(hours=1)
    if text in ("this week", "last week", "past week"):
        return now - timedelta(days=7)
    m = _REL_RE.match(text)
    if m:
        seconds = float(m.group(1)) * _REL_UNITS[m.group(2).lower()]
        return now - timedelta(seconds=min(seconds, 3650 * 86400))
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"cannot understand time {value!r}; use ISO-8601, 'today', 'yesterday' or '2h'"
        ) from None
    return when.astimezone() if when.tzinfo is None else when


def _parse_roi(value: Any) -> tuple[float, float, float, float] | None:
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


def _parse_schedule(value: Any) -> Schedule | None:
    if value is None:
        return None
    try:
        return Schedule.from_dict(value)
    except RuleValidationError as exc:
        raise ToolError(f"invalid schedule: {exc}") from None


def _event_dict(event: EventRecord, rule: RuleSpec | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": event.id,
        "rule_id": event.rule_id,
        "time": _local(event.ts),
        "ts_utc": event.ts,
        "caption": event.caption,
        "confidence": round(event.confidence, 2) if event.confidence is not None else None,
        "source": event.source,
        "feedback": event.feedback,
    }
    if rule is not None:
        out["rule"] = rule.text or rule.question
    if event.snapshot_path:
        out["media"] = f"MEDIA:{event.snapshot_path}"
    return out


def _rule_brief(spec: RuleSpec) -> dict[str, Any]:
    return {
        "rule_id": spec.id,
        "watching_for": spec.text or spec.question,
        "question": spec.question,
        "mode": spec.mode,
        "hold_seconds": spec.hold_seconds,
        "cooldown_seconds": spec.cooldown_seconds,
        "source": spec.source,
        "roi": list(spec.roi) if spec.roi else None,
        "schedule": spec.schedule.to_dict() if spec.schedule else None,
        "action": spec.action,
        "action_prompt": spec.action_prompt or None,
        "confidence_threshold": round(spec.confidence_threshold, 3),
        "enabled": spec.enabled,
        "recipe": spec.recipe,
        "motion": spec.motion,
        "compiled_by": spec.compiled_by,
    }


def _caps_text(cfg: SentinelConfig) -> str:
    hour = cfg.vision_calls_per_hour
    return "no cap" if hour is None else f"{hour} vision checks/hour"


def _decode_to_jpeg(data: bytes) -> bytes:
    """Any image cv2 can read as a JPEG whose longest side is at most ``EXAMPLE_MAX_SIDE``
    (teaching examples). Without OpenCV only JPEG input is accepted, stored unchanged."""
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        if data.startswith(b"\xff\xd8\xff"):
            return data
        raise ToolError("only JPEG images can be used without OpenCV installed") from None
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ToolError("the file is not an image Sentinel can read")
    jpeg = encode_jpeg(img, max_side=EXAMPLE_MAX_SIDE, quality=EXAMPLE_JPEG_QUALITY)
    if jpeg is None:
        raise ToolError("could not convert the image to JPEG")
    return jpeg


def _small_jpeg(data: bytes) -> bytes:
    """``data`` re-encoded to the example size when that works, else unchanged. Never raises."""
    try:
        return _decode_to_jpeg(data)
    except ToolError:
        return data


class _HourlyCap:
    """Tiny thread-safe rolling-hour counter."""

    def __init__(self, cap: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.cap = max(0, int(cap))
        self._clock = clock
        self._lock = threading.Lock()
        self._times: list[float] = []

    def take(self) -> bool:
        now = self._clock()
        with self._lock:
            self._times = [t for t in self._times if now - t < 3600.0]
            if len(self._times) >= self.cap:
                return False
            self._times.append(now)
            return True


# --------------------------------------------------------------------------- handlers


class SentinelTools:
    """The nine tool handlers, bound to one :class:`Sentinel`."""

    def __init__(
        self,
        sentinel: Sentinel,
        *,
        compile_per_hour: int = COMPILE_CALLS_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
        owner_origin: Mapping[str, Any] | None = None,
    ) -> None:
        self.s = sentinel
        #: When set, every call acts as this fixed (local) origin; used by the dashboard API.
        self._owner_origin = dict(owner_origin) if owner_origin is not None else None
        self._compile_cap = _HourlyCap(compile_per_hour, clock)
        self._asked_here: set[str] = set()
        self._asked_lock = threading.Lock()

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {name: self._wrap(name, getattr(self, name)) for name in TOOL_NAMES}

    def _wrap(self, name: str, fn: Callable[..., str]) -> Callable[..., str]:
        def handler(args: dict, **kwargs: Any) -> str:
            if args is None:
                args = {}
            if not isinstance(args, Mapping):
                return _err("arguments must be a JSON object")
            self.s.prime_secrets()
            try:
                return fn(dict(args), **kwargs)
            except ToolError as exc:
                return _err(str(exc))
            except SourceSpecError as exc:
                return _err(
                    f"invalid source: {exc}. Examples: camera:0, screen, rtsp://..., "
                    "folder:/path. Use sentinel_sources to list what is available."
                )
            except SourceError as exc:
                return _err(f"could not read the source: {exc}")
            except RuleValidationError as exc:
                return _err(f"invalid rule: {exc}")
            except StoreError as exc:
                return _err(f"Sentinel storage is unavailable: {exc}")
            except Exception as exc:  # noqa: BLE001 - a tool must answer, never crash the turn
                logger.exception("sentinel: tool %s failed", name)
                return _err(f"unexpected error ({type(exc).__name__}); see the Hermes log")

        handler.__name__ = f"handle_{name}"
        handler.__doc__ = SCHEMAS[name]["description"]
        return handler

    # -- helpers

    def _source_arg(self, args: Mapping[str, Any]) -> str:
        source = _str_arg(args, "source", limit=2048) or self.s.config.default_source
        SourceSpec.parse(source, named_streams=self.s.config.stream_urls)  # validate early
        return source

    def _caller(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The calling session's origin (``capture_origin``): who may see which rules."""
        if self._owner_origin is not None:
            return dict(self._owner_origin)
        return capture_origin(kwargs, self.s.ctx, routes=self.s.routes)

    def _rule(self, rule_id: str, caller: Mapping[str, Any] | None = None) -> RuleSpec:
        spec = self.s.store.get_rule(rule_id)
        if (
            spec is not None
            and caller is not None
            and not visible_to(spec.origin, caller, self.s.routes)
        ):
            spec = None  # another chat's rule: invisible, not "forbidden"
        if spec is None:
            raise ToolError(f"no rule with id {rule_id!r}; use sentinel_rules to list rules")
        return spec

    def _save_and_apply(self, spec: RuleSpec) -> RuleSpec:
        saved = self.s.store.upsert_rule(spec)
        engine = self.s.engine
        if saved.enabled:
            engine.add_rule(saved)
        else:
            engine.update_rule(saved)
        return saved

    # -- tools

    def sentinel_sources(self, args: dict, **_: Any) -> str:
        probe = args.get("probe_cameras", True)
        infos = list_sources(named_streams=self.s.config.stream_urls, probe_cameras=bool(probe))
        sources = [i.to_dict() for i in infos]
        return _ok(
            sources=sources,
            default_source=self.s.config.default_source,
            hint="Also possible: 'snapshot:https://host/cam.jpg', 'folder:/path/to/images', "
            "'screen:1@x,y,w,h' for part of a screen.",
        )

    def sentinel_setup_check(self, args: dict, **kwargs: Any) -> str:
        test = None
        if args.get("test_delivery") is True:
            test = run_delivery_test(self.s.alerter, self._caller(kwargs))
        checks = run_checks(SetupEnv.from_sentinel(self.s, kwargs))
        extra: dict[str, Any] = {}
        report = format_report(checks)
        if test is not None:
            extra["delivery_test"] = {"delivered": test.delivered, "via": test.via}
            report += "\n" + format_delivery_test(test)
        return _ok(
            checks=[c.to_dict() for c in checks], report=report, **summarize(checks), **extra
        )

    def _setup_hint(self) -> str:
        """Point at ``/sentinel setup`` while a fail-level check exists (cheap checks only)."""
        try:
            env = SetupEnv.from_sentinel(self.s, probe_sources=False)
            failing = fail_titles(run_checks(env))
        except Exception:  # noqa: BLE001 - a hint must never break a watch
            return ""
        if not failing:
            return ""
        return (
            f" Setup problem ({', '.join(failing)}): run /sentinel setup "
            "(or the sentinel_setup_check tool) for the fix."
        )

    def sentinel_watch(self, args: dict, **kwargs: Any) -> str:
        cfg = self.s.config
        source = self._source_arg(args)
        recipe_name = _str_arg(args, "recipe", limit=64)
        what = _str_arg(args, "what")
        roi = _parse_roi(args.get("roi"))
        action_prompt = _str_arg(args, "action_prompt")
        existing = self.s.store.list_rules()
        if len(existing) >= MAX_RULES:
            raise ToolError(f"too many rules ({MAX_RULES}); delete some with sentinel_update")
        origin = self._caller(kwargs)
        if recipe_name:
            recipe = get_recipe(recipe_name)
            if recipe is None:
                raise ToolError(
                    f"unknown recipe {recipe_name!r}; choose one of " + ", ".join(recipe_names())
                )
            spec = build_rule(
                recipe,
                source,
                text=what,
                defaults=cfg.rule_defaults(),
                origin=origin,
                roi=roi,
                action_prompt=action_prompt,
            )
        else:
            if not what:
                raise ToolError("say what to watch for in 'what' (or pick a 'recipe')")
            if not self._compile_cap.take():
                raise ToolError(
                    f"rule compiling is limited to {self._compile_cap.cap} per hour; use a "
                    "recipe or try again later"
                )
            spec = compile_rule(
                what,
                source,
                getattr(self.s.ctx, "llm", None),
                defaults=cfg.rule_defaults(),
                origin=origin,
            )
            changes: dict[str, Any] = {}
            if roi is not None:
                changes["roi"] = roi
            if action_prompt:
                changes.update(action=ACTION_AGENT, action_prompt=action_prompt)
            if changes:
                spec = spec.with_changes(**changes)
        changes = {}
        hold = _num_arg(args, "hold_seconds")
        if hold is not None:
            changes["hold_seconds"] = hold
            if hold > 0 and spec.mode == "trigger":
                changes["mode"] = "condition"
        cooldown = _num_arg(args, "cooldown_seconds")
        if cooldown is not None:
            changes["cooldown_seconds"] = cooldown
        if args.get("schedule") is not None:
            changes["schedule"] = _parse_schedule(args.get("schedule"))
        if args.get("motion") is not None:
            changes["motion"] = _bool_arg(args, "motion")
        if changes:
            spec = spec.with_changes(**changes)
        saved = self._save_and_apply(spec)
        engine = self.s.engine
        hold_text = (
            f" once it has held for {saved.hold_seconds:g}s" if saved.effective_hold_seconds else ""
        )
        message = (
            f'Watching "{saved.source}" for: "{saved.question}"{hold_text}. Checks only when '
            f"something moves. Cap: {_caps_text(cfg)}."
        )
        if engine.standing_by:
            message += " (Another Hermes process is running the watcher; it will pick this up.)"
        message += (
            " Alerts are injected into the chat, which Hermes only allows outside the classic "
            "CLI when the user has set this in config.yaml (it is off by default):\n"
            + GATEWAY_INJECTION_YAML
        )
        message += self._here_hint(origin, kwargs)
        message += self._setup_hint()
        return _ok(rule=_rule_brief(saved), message=message)

    def _here_hint(self, origin: Mapping[str, Any], kwargs: Mapping[str, Any]) -> str:
        """Ask for ``/sentinel here`` once per session while the routing key is unknown."""
        if origin.get("session_key") or origin.get("chat_id"):
            return ""
        who = str(origin.get("session_id") or kwargs.get("session_id") or "?")
        with self._asked_lock:
            if who in self._asked_here:
                return ""
            self._asked_here.add(who)
            if len(self._asked_here) > 1024:
                self._asked_here.clear()
        where = "this chat" if is_chat_origin(origin) else "this session"
        return (
            f" To make sure alerts come back to {where}, ask the user to send "
            "'/sentinel here' once."
        )

    @staticmethod
    def status_text(spec: RuleSpec, engine: Any, status: Mapping[str, Any] | None) -> str:
        """Human status of a rule: paused, not running, starting, source offline, or state."""
        if not spec.enabled:
            return "paused"
        if status is None or not engine.running:
            return "starting" if engine and engine.running else "not running"
        if status.get("source_online") is False:
            return f"source offline: {status.get('source_error') or 'unknown'}"
        return str(status.get("state", "idle"))

    def sentinel_rules(self, args: dict, **kwargs: Any) -> str:
        rule_id = _str_arg(args, "rule_id", limit=64)
        store = self.s.store
        caller = self._caller(kwargs)
        specs = (
            [self._rule(rule_id, caller)]
            if rule_id
            else [r for r in store.list_rules() if visible_to(r.origin, caller, self.s.routes)]
        )
        engine = self.s.engine_if_built
        out = []
        for spec in specs:
            entry = _rule_brief(spec)
            hint = route_hint(spec.origin, caller)
            if hint:
                entry["hint"] = hint
            status = engine.rule_status(spec.id) if engine is not None else None
            entry["status"] = self.status_text(spec, engine, status)
            if status:
                entry["checks_since_load"] = status.get("checks")
                entry["last_checked"] = status.get("last_checked")
                verdict = status.get("last_verdict") or {}
                if verdict.get("error"):
                    entry["last_error"] = verdict["error"]
            hour, day = store.usage_totals(rule_id=spec.id)
            entry["vision_checks"] = {"this_hour": hour, "last_24h": day}
            last = store.last_event(spec.id)
            entry["last_event"] = _event_dict(last) if last else None
            entry["events_total"] = store.count_events(rule_id=spec.id)
            out.append(entry)
        extra: dict[str, Any] = {}
        blocked = self.s.alerter.delivery_blocked()
        if blocked:
            extra["delivery_blocked"] = blocked
        return _ok(rules=out, count=len(out), budget=self.s.budget.status().describe(), **extra)

    def sentinel_update(self, args: dict, **kwargs: Any) -> str:
        rule_id = _str_arg(args, "rule_id", required=True, limit=64)
        action = _str_arg(args, "action", required=True, limit=16).lower()
        spec = self._rule(rule_id, self._caller(kwargs))
        engine = self.s.engine
        if action == "pause":
            saved = self._save_and_apply(spec.with_changes(enabled=False))
            return _ok(rule=_rule_brief(saved), message=f"Paused rule {rule_id}.")
        if action == "resume":
            saved = self._save_and_apply(spec.with_changes(enabled=True))
            return _ok(rule=_rule_brief(saved), message=f"Resumed rule {rule_id}.")
        if action == "delete":
            purge = bool(args.get("purge_events", False))
            engine.remove_rule(rule_id)
            self.s.store.delete_rule(rule_id, purge_events=purge)
            return _ok(
                message=f"Deleted rule {rule_id}"
                + (" and its events." if purge else "; its events are kept.")
            )
        if action != "edit":
            raise ToolError("action must be pause, resume, delete or edit")
        raw = args.get("changes")
        if not isinstance(raw, Mapping) or not raw:
            raise ToolError("action=edit needs a non-empty 'changes' object")
        allowed = {
            "question",
            "mode",
            "hold_seconds",
            "cooldown_seconds",
            "confidence_threshold",
            "roi",
            "source",
            "action_prompt",
            "schedule",
            "motion",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ToolError(f"cannot change {unknown}; allowed: {sorted(allowed)}")
        changes: dict[str, Any] = {}
        for key in ("hold_seconds", "cooldown_seconds", "confidence_threshold"):
            if key in raw:
                changes[key] = _num_arg(raw, key)
        if "question" in raw:
            changes["question"] = _str_arg(raw, "question", required=True, limit=500)
        if "mode" in raw:
            mode = _str_arg(raw, "mode", required=True, limit=16)
            if mode not in MODES:
                raise ToolError(f"mode must be one of {list(MODES)}")
            changes["mode"] = mode
        if "roi" in raw:
            changes["roi"] = _parse_roi(raw.get("roi"))
        if "source" in raw:
            changes["source"] = self._source_arg(raw)
        if "action_prompt" in raw:
            prompt = _str_arg(raw, "action_prompt")
            changes["action_prompt"] = prompt
            changes["action"] = ACTION_AGENT if prompt else ACTION_NOTIFY
        if "schedule" in raw:
            changes["schedule"] = _parse_schedule(raw.get("schedule"))
        if "motion" in raw:
            changes["motion"] = _bool_arg(raw, "motion")
        saved = self._save_and_apply(spec.with_changes(**changes))
        return _ok(rule=_rule_brief(saved), message=f"Updated rule {rule_id}.")

    def sentinel_look(self, args: dict, **_: Any) -> str:
        source = self._source_arg(args)
        question = _str_arg(args, "question", limit=500)
        budget = self.s.budget
        frame = self.s.engine.grab_frame(source)
        if not budget.try_consume(rule_id="look"):
            raise ToolError(budget.status().describe())
        result = self.s.judge.describe(frame, question or None)
        media = None
        jpeg = encode_jpeg(frame)
        if jpeg is not None:
            try:
                media = f"MEDIA:{self.s.store.save_snapshot(jpeg, rule_id='look')}"
            except Exception as exc:  # noqa: BLE001 - the answer matters more than the photo
                logger.warning("sentinel: could not save look snapshot: %s", exc)
        if not result.ok:
            return _err(f"the vision model could not answer: {result.error}", media=media)
        return _ok(source=source, answer=result.answer, caption=result.caption, media=media)

    def sentinel_events(self, args: dict, **kwargs: Any) -> str:
        text = _str_arg(args, "text", limit=500)
        rule_id = _str_arg(args, "rule_id", limit=64) or None
        feedback = _str_arg(args, "feedback", limit=16) or None
        if feedback is not None and feedback not in ("correct", "false_alarm"):
            raise ToolError("feedback must be 'correct' or 'false_alarm'")
        limit = _int_arg(args, "limit") or 20
        limit = max(1, min(limit, 100))
        since = parse_time_bound(args.get("since"))
        until = parse_time_bound(args.get("until"))
        store = self.s.store
        caller = self._caller(kwargs)
        scoped = is_chat_origin(caller)
        if rule_id and scoped:
            self._rule(rule_id, caller)  # not this chat's rule: "no rule"
        events = store.query_events(
            text=text or None,
            rule_id=rule_id,
            since=since.astimezone(UTC) if since else None,
            until=until.astimezone(UTC) if until else None,
            feedback=feedback,
            limit=limit,
        )
        rules = {s.id: s for s in store.list_rules()}
        if scoped:  # messaging chats only see events of their own rules
            events = [
                e
                for e in events
                if e.rule_id in rules and visible_to(rules[e.rule_id].origin, caller, self.s.routes)
            ]
        items = [_event_dict(e, rules.get(e.rule_id)) for e in events]
        summary = "; ".join(f"{i['time'][11:16]} {i['caption']}" for i in items[:10])
        return _ok(count=len(items), events=items, summary=summary or "No matching events.")

    def sentinel_feedback(self, args: dict, **kwargs: Any) -> str:
        event_id = _int_arg(args, "event_id", required=True)
        verdict = _str_arg(args, "verdict", required=True, limit=16).lower().replace(" ", "_")
        if verdict in ("false", "wrong", "falsealarm"):
            verdict = "false_alarm"
        if verdict not in ("correct", "false_alarm"):
            raise ToolError("verdict must be 'correct' or 'false_alarm'")
        note = _str_arg(args, "note", limit=500)
        store = self.s.store
        event = store.get_event(int(event_id))  # type: ignore[arg-type]
        if event is None:
            raise ToolError(f"no event #{event_id}; use sentinel_events to find it")
        caller = self._caller(kwargs)
        if is_chat_origin(caller):
            owner = store.get_rule(event.rule_id)
            if owner is None or not visible_to(owner.origin, caller, self.s.routes):
                raise ToolError(f"no event #{event_id}; use sentinel_events to find it")
        if event.feedback == verdict:  # idempotent: no second example, no second nudge
            current = store.get_rule(event.rule_id)
            return _ok(
                event_id=event.id,
                feedback=verdict,
                already_recorded=True,
                rule_id=event.rule_id,
                **({"confidence_threshold": current.confidence_threshold} if current else {}),
                message=f"Event #{event.id} was already marked as {verdict.replace('_', ' ')}; "
                "nothing changed.",
            )
        store.set_feedback(event.id, verdict)
        result: dict[str, Any] = {"event_id": event.id, "feedback": verdict}
        spec = store.get_rule(event.rule_id)
        if spec is None:
            return _ok(**result, message="Feedback saved (the rule no longer exists).")
        kind = "neg" if verdict == "false_alarm" else "pos"
        if event.snapshot_path:
            try:
                example = self._example_from_event(spec.id, kind, event, note)
                result["example_id"] = example.id
            except (StoreError, ValueError, OSError) as exc:
                result["example_error"] = str(exc)[:200]
        if verdict == "false_alarm":
            spec = self._retune(spec, raised_threshold(spec.confidence_threshold))
            result["confidence_threshold"] = spec.confidence_threshold
            message = (
                f"Marked event #{event.id} as a false alarm. Saved it as a 'no' example "
                f"and raised the rule's confidence threshold to "
                f"{spec.confidence_threshold:.2f}."
            )
        else:
            before = spec.confidence_threshold
            spec = self._retune(spec, lowered_threshold(before))
            result["confidence_threshold"] = spec.confidence_threshold
            message = f"Marked event #{event.id} as correct and saved it as a 'yes' example."
            if spec.confidence_threshold < before:
                message += (
                    f" Nudged the rule's confidence threshold down to "
                    f"{spec.confidence_threshold:.2f}."
                )
        return _ok(**result, rule_id=spec.id, message=message)

    def _retune(self, spec: RuleSpec, threshold: float) -> RuleSpec:
        """Save ``spec`` with a new confidence threshold when it differs."""
        if threshold == spec.confidence_threshold:
            return spec
        return self._save_and_apply(spec.with_changes(confidence_threshold=threshold))

    def _example_from_event(self, rule_id: str, kind: str, event: EventRecord, note: str) -> Any:
        """Add an event's photo as an example, shrunk to the example size when possible."""
        store = self.s.store
        data = None
        try:
            if event.snapshot_path:
                data = Path(event.snapshot_path).read_bytes()
        except OSError:
            data = None
        if data:
            return store.add_example(rule_id, kind, _small_jpeg(data), note=note)
        return store.add_example(rule_id, kind, event_id=event.id, note=note)

    def sentinel_teach(self, args: dict, **kwargs: Any) -> str:
        rule_id = _str_arg(args, "rule_id", required=True, limit=64)
        raw_kind = _str_arg(args, "kind", required=True, limit=16).lower()
        kinds = {
            "yes": "pos",
            "pos": "pos",
            "positive": "pos",
            "no": "neg",
            "neg": "neg",
            "negative": "neg",
        }
        if raw_kind not in kinds:
            raise ToolError("kind must be 'yes' (should trigger) or 'no' (should not)")
        kind = kinds[raw_kind]
        note = _str_arg(args, "note", limit=500)
        spec = self._rule(rule_id, self._caller(kwargs))
        store = self.s.store
        image_path = _str_arg(args, "image_path", limit=4096)
        event_id = _int_arg(args, "event_id")
        from_source = bool(args.get("from_source", False))
        given = sum(bool(x) for x in (image_path, event_id is not None, from_source))
        if given != 1:
            raise ToolError("give exactly one of image_path, event_id or from_source=true")
        if event_id is not None:
            event = store.get_event(event_id)
            caller = self._caller(kwargs)
            owner = store.get_rule(event.rule_id) if event is not None else None
            if event is None or (
                is_chat_origin(caller)
                and (owner is None or not visible_to(owner.origin, caller, self.s.routes))
            ):
                raise ToolError(f"no event #{event_id}")
            example = self._example_from_event(spec.id, kind, event, note)
        elif from_source:
            frame = self.s.engine.grab_frame(spec.source)
            jpeg = encode_jpeg(frame, max_side=EXAMPLE_MAX_SIDE, quality=EXAMPLE_JPEG_QUALITY)
            if jpeg is None:
                raise ToolError("could not encode the current frame (is OpenCV installed?)")
            example = store.add_example(spec.id, kind, jpeg, note=note)
        else:
            path = Path(image_path).expanduser()
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ToolError("image_path must be a .jpg, .jpeg, .png, .bmp or .webp file")
            try:
                if not path.is_file():
                    raise ToolError(f"no image file at {image_path}")
                if path.stat().st_size > MAX_IMAGE_FILE_BYTES:
                    raise ToolError("the image is larger than 20 MB")
                data = path.read_bytes()
            except OSError as exc:
                raise ToolError(f"cannot read {image_path}: {exc.strerror or exc}") from None
            example = store.add_example(spec.id, kind, _decode_to_jpeg(data), note=note)
        counts = {k: len(store.list_examples(spec.id, k)) for k in ("pos", "neg")}
        word = "yes" if kind == "pos" else "no"
        return _ok(
            example_id=example.id,
            rule_id=spec.id,
            kind=word,
            examples={"yes": counts["pos"], "no": counts["neg"]},
            message=f"Added a '{word}' example to rule {spec.id}. The two newest of each kind "
            "are shown to the vision model with every check.",
        )


def register_tools(ctx: Any, sentinel: Sentinel) -> list[str]:
    """Register all tools on ``ctx``; returns the names registered."""
    handlers = SentinelTools(sentinel).handlers()
    names = []
    for name in TOOL_NAMES:
        handle = ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=SCHEMAS[name],
            handler=handlers[name],
            description=SCHEMAS[name]["description"],
            emoji=EMOJI.get(name, ""),
        )
        if handle is not None:
            names.append(name)
    return names
