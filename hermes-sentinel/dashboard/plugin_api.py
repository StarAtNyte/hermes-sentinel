"""Sentinel dashboard / Desktop backend, mounted by Hermes at /api/plugins/hermes-sentinel/.

A thin HTTP view over the *same* runtime the agent tools use (``Sentinel``: store, budget,
engine, ``SentinelTools``). It adds no rule logic of its own: pause / resume / delete, feedback
and watch all go through ``SentinelTools`` handlers; reads come from the store and the engine's
in-memory capture state.

Hermes imports this file on its own (not as part of the plugin package), so the plugin modules
are reached through the package name Hermes' loader gives the plugin. If the plugin is not
loaded in this process every read degrades to an empty answer and every write returns 503.

Rules of the road: dashboard callers are the local owner (no messaging-origin scoping);
responses never carry credentials (source specs are redacted); request bodies and limits are
bounded; no handler touches a camera (frames come from the capture thread's latest frame) and
blocking work runs in worker threads.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("hermes_sentinel.dashboard")

router = APIRouter()

PACKAGE = "hermes_plugins.hermes_sentinel"
OWNER_ORIGIN = {"surface": "desktop"}  # the dashboard user owns every rule
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT = 2000
MAX_SOURCE = 2048
MAX_KEY = 512
MAX_CAPTION = 600
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
FRAME_MAX_SIDE = 640
FRAME_QUALITY = 75
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INT_RE = re.compile(r"^[0-9]{1,12}$")
_VERDICTS = ("correct", "false_alarm")
_NO_STORE = {"Cache-Control": "no-store"}
_NOT_LOADED = (
    "Sentinel is not loaded in this process: the Desktop pane must be served by the same "
    "Hermes process that runs the agent and loaded the hermes-sentinel plugin"
)
_NAMESPACE_SUFFIX = "__home_"  # Hermes names a second-scope plugin hermes_sentinel__home_<digest>
MAX_DATA_URL_BYTES = 4 * 1024 * 1024  # larger images are not inlined


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status, headers=_NO_STORE)


def _json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


# --------------------------------------------------------------------------- plugin access


def _package_names() -> list[str]:
    """Names of loaded Sentinel plugin packages (Hermes names vary by profile scope)."""
    return sorted(
        k for k in list(sys.modules)
        if k == PACKAGE or k.startswith(PACKAGE + _NAMESPACE_SUFFIX)
    )


def _active() -> tuple[str, Any] | None:
    """``(package name, Sentinel)`` of the loaded plugin whose runtime has a live Sentinel."""
    for name in _package_names():
        try:
            runtime = importlib.import_module(f"{name}.sentinel.runtime")
            sentinel = runtime.active_sentinel()
        except Exception:  # noqa: BLE001 - partially imported / foreign module
            continue
        if sentinel is not None:
            return name, sentinel
    return None


def _package() -> str:
    found = _active()
    if found is not None:
        return found[0]
    names = _package_names()
    return names[0] if names else PACKAGE


def _mod(name: str) -> Any:
    return importlib.import_module(f"{_package()}.sentinel.{name}")


def _sentinel() -> Any | None:
    """The loaded :class:`Sentinel`, or ``None`` when the plugin is not loaded here."""
    found = _active()
    return found[1] if found is not None else None


_TOOLS: dict[int, Any] = {}
_TOOLS_LOCK = threading.Lock()


def _tools(sentinel: Any) -> Any:
    """One ``SentinelTools`` per Sentinel (keeps its compile-rate cap), fixed local owner."""
    with _TOOLS_LOCK:
        entry = _TOOLS.get(id(sentinel))
        if entry is None or entry[0] is not sentinel:
            if len(_TOOLS) > 8:
                _TOOLS.clear()
            entry = (sentinel, _mod("tools").SentinelTools(sentinel, owner_origin=OWNER_ORIGIN))
            _TOOLS[id(sentinel)] = entry
        return entry[1]


def _need() -> Any:
    sentinel = _sentinel()
    if sentinel is None:
        raise ApiError(503, _NOT_LOADED)
    return sentinel


def _version() -> str:
    try:
        return str(importlib.import_module(_package()).__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


async def _run(fn: Any, *args: Any) -> Any:
    """Run ``fn`` in a worker thread; map known failures to ``{ok:false,error}`` responses."""
    try:
        return await asyncio.to_thread(fn, *args)
    except ApiError as exc:
        return _fail(exc.status, exc.message)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name == "StoreError":
            return _fail(503, "Sentinel storage is unavailable")
        logger.exception("sentinel dashboard: %s failed", fn.__name__)
        return _fail(500, f"unexpected error ({name})")


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "request body too large")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError(400, "body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def _opt_str(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"'{key}' must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ApiError(400, f"'{key}' is too long (max {limit} characters)")
    return value


def _check_rule_id(rule_id: str) -> str:
    if not _ID_RE.match(rule_id or ""):
        raise ApiError(400, "invalid rule id")
    return rule_id


def _check_event_id(event_id: str) -> int:
    if not _INT_RE.match(event_id or ""):
        raise ApiError(400, "invalid event id")
    return int(event_id)


# --------------------------------------------------------------------------- masking


_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*:)(?=[A-Za-z][A-Za-z0-9+.-]*://)")
_GENERIC_SEGMENT_RE = re.compile(
    r"^(?:stream|ch|channel|cam|camera|video|profile|live|main|sub|h26[45]|streaming|channels|"
    r"media|unicast|onvif)\d*$|^\d{1,4}$", re.IGNORECASE)
_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/?#@]*)@")


def _mask_word(redact: Any, word: str) -> str:
    """Redact one ``scheme://`` word, including ``snapshot:https://...`` style prefixes."""
    prefix = ""
    m = _PREFIX_RE.match(word)
    if m:
        prefix, word = m.group(1), word[m.end():]
    word = redact(word)
    m = _USERINFO_RE.match(word)
    if m and "${" not in m.group(2):
        word = f"{m.group(1)}***@{word[m.end():]}"  # token-only userinfo
    # RTSP URLs often carry the secret in the path (rtsp://h/live/<token>). Keep only generic
    # path segments (stream1, ch2, live, 101...); otherwise show a short digest so two
    # cameras on one recorder stay distinguishable without revealing the path.
    low = word.lower()
    if low.startswith(("rtsp://", "rtsps://")) and "${" not in word:
        scheme, _, rest = word.partition("://")
        hostpart, slash, tail = rest.partition("/")
        path = tail.split("?", 1)[0]
        segments = [seg for seg in path.split("/") if seg]
        if slash and segments and not all(_GENERIC_SEGMENT_RE.match(seg) for seg in segments):
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:4]
            query = f"?{tail.split('?', 1)[1]}" if "?" in tail else ""
            word = f"{scheme}://{hostpart}/***~{digest}{query}"
    return prefix + word


def _mask(text: Any) -> str:
    """Make a source spec / error string safe to return: no URL credentials, bounded."""
    redact = _mod("sources").redact_url
    out = " ".join(
        _mask_word(redact, w) if "://" in w else w for w in str(text or "").split(" ")
    )
    return out[:MAX_SOURCE]


def _source_key(sentinel: Any, spec: str) -> str:
    engine = sentinel.engine_if_built
    try:
        key = engine.source_key(spec) if engine is not None else _mod("engine").default_source_key(
            spec, sentinel.config.stream_urls)
    except Exception:  # noqa: BLE001
        key = str(spec).strip()
    return _mask(key)


def _source_label_kind(spec: str, named: Any) -> tuple[str, str]:
    sources = _mod("sources")
    try:
        parsed = sources.SourceSpec.parse(spec, named_streams=named)
        return _mask(parsed.display_name), str(parsed.kind)
    except Exception:  # noqa: BLE001
        return _mask(spec), "unknown"


# --------------------------------------------------------------------------- shapes


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _rule_item(sentinel: Any, spec: Any) -> dict[str, Any]:
    store = sentinel.store
    engine = sentinel.engine_if_built
    status = engine.rule_status(spec.id) if engine is not None else None
    text = _tools(sentinel).status_text(spec, engine, status)
    last = store.last_event(spec.id)
    hour, _day = store.usage_totals(rule_id=spec.id)
    return {
        "id": spec.id,
        "text": (spec.text or spec.question or "")[:MAX_TEXT],
        "source": _mask(spec.source),
        "mode": spec.mode,
        "status": _mask(text),
        "paused": not spec.enabled,
        "last_event_at": last.ts if last else None,
        "vision_calls_hour": hour,
        "cooldown_s": spec.cooldown_seconds,
        "hold_s": spec.hold_seconds,
    }


def _event_item(event: Any, rules: dict[str, Any]) -> dict[str, Any]:
    rule = rules.get(event.rule_id)
    path = event.snapshot_path
    return {
        "id": event.id,
        "rule_id": event.rule_id,
        "rule_text": ((rule.text or rule.question) if rule else "")[:MAX_TEXT],
        "ts": event.ts,
        "caption": (event.caption or "")[:MAX_CAPTION],
        "confidence": round(event.confidence, 3) if event.confidence is not None else None,
        "feedback": event.feedback,
        "has_snapshot": bool(path) and Path(path).is_file(),
    }


def _tool(sentinel: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Call a SentinelTools handler; raise :class:`ApiError` with a fitting status on failure."""
    result = json.loads(_tools(sentinel).handlers()[name](args))
    if not result.get("ok"):
        message = _mask(result.get("error") or "request failed")
        low = message.lower()
        if low.startswith(("no rule", "no event")):
            raise ApiError(404, message)
        if "limited to" in low:
            raise ApiError(429, message)
        if low.startswith(("sentinel storage", "unexpected")):
            raise ApiError(503 if "storage" in low else 500, message)
        raise ApiError(400, message)
    return result


def _known_rule(sentinel: Any, rule_id: str) -> Any:
    spec = sentinel.store.get_rule(rule_id)
    if spec is None:
        raise ApiError(404, f"no rule with id {rule_id!r}")
    return spec


# --------------------------------------------------------------------------- sources


def _collect_sources(sentinel: Any) -> list[dict[str, Any]]:
    """Sources in use by rules, merged with the capture workers' in-memory state."""
    rules = sentinel.store.list_rules()
    engine = sentinel.engine_if_built
    running = bool(engine is not None and engine.running)
    infos = engine.source_infos() if running else {}
    named = sentinel.config.stream_urls
    groups: dict[str, dict[str, Any]] = {}
    for spec in rules:
        key = _source_key(sentinel, spec.source)
        group = groups.setdefault(key, {"spec": spec.source, "rules": 0})
        group["rules"] += 1
    for key in infos:
        groups.setdefault(_mask(key), {"spec": key, "rules": len(infos[key]["rules"])})
    internal = {_mask(k): k for k in infos}
    out = []
    for key in sorted(groups):
        info = infos.get(internal.get(key, key))
        label, kind = _source_label_kind(groups[key]["spec"], named)
        if info is not None:
            online, age, error = info["online"], info["last_frame_age_s"], info["error"]
        else:
            online, age = False, None
            error = None if running else (
                "another Hermes process is running the watcher"
                if engine is not None and engine.standing_by else "watcher is not running")
        out.append({
            "key": key, "label": label, "kind": kind, "online": online,
            "last_frame_age_s": age, "rules": groups[key]["rules"],
            "error": _mask(error) if error else None,
        })
    return out


# --------------------------------------------------------------------------- routes


def _status() -> dict[str, Any]:
    sentinel = _sentinel()
    base: dict[str, Any] = {
        "engine": {"running": False, "standby": False, "since": None},
        "delivery": {"blocked": False, "reason": None},
        "budget": {"hour_used": 0, "hour_cap": None, "day_used": 0, "day_cap": None,
                   "paused": False},
        "sources": 0,
        "rules": 0,
        "version": _version(),
    }
    if sentinel is None:
        base["note"] = _NOT_LOADED
        return base
    engine = sentinel.engine_if_built
    if engine is not None:
        base["engine"] = {
            "running": bool(engine.running),
            "standby": bool(engine.standing_by),
            "since": _iso(engine.started_at) if engine.running else None,
        }
    reason = sentinel.alerter.delivery_blocked()
    base["delivery"] = {"blocked": bool(reason), "reason": reason}
    cfg = sentinel.config
    base["budget"].update(hour_cap=cfg.vision_calls_per_hour, day_cap=cfg.vision_calls_per_day,
                          paused=cfg.vision_calls_per_hour == 0)
    try:
        st = sentinel.budget.status()
        base["budget"] = {
            "hour_used": st.hour_used, "hour_cap": st.hour_cap, "day_used": st.day_used,
            "day_cap": st.day_cap, "paused": bool(st.exhausted or st.hour_cap == 0),
        }
        base["rules"] = len(sentinel.store.list_rules())
        base["sources"] = len(_collect_sources(sentinel))
    except Exception as exc:  # noqa: BLE001 - storage down: report what we know
        base["note"] = _mask(f"storage unavailable: {type(exc).__name__}")
    return base


@router.get("/status")
async def status() -> Any:
    return _json(await _run(_status))


def _list_sources() -> list[dict[str, Any]]:
    sentinel = _sentinel()
    return _collect_sources(sentinel) if sentinel is not None else []


@router.get("/sources")
async def sources() -> Any:
    result = await _run(_list_sources)
    return result if isinstance(result, Response) else _json(result)


def _frame(source_key: str) -> bytes:
    if not source_key or len(source_key) > MAX_KEY:
        raise ApiError(400, "invalid source key")
    sentinel = _need()
    engine = sentinel.engine_if_built
    if engine is None or not engine.running:
        raise ApiError(404, "no frame available: the watcher is not running")
    # Clients may percent-encode the key (it contains %2A after redaction), and proxies may
    # decode once more: compare the decoded forms.
    internal = {unquote(_mask(k)): k for k in engine.source_infos()}
    key = internal.get(unquote(source_key))
    frame = engine.latest_frame(key) if key is not None else None
    if frame is None:
        raise ApiError(404, "no frame available for this source yet")
    data = _mod("judge").encode_jpeg(frame, max_side=FRAME_MAX_SIDE, quality=FRAME_QUALITY)
    if data is None:
        raise ApiError(404, "could not encode the frame")
    return data


def _image_response(data: bytes, encoding: str | None, headers: dict[str, str]) -> Response:
    """Raw JPEG by default; ``encoding=data_url`` returns ``{ok, data_url}`` JSON instead.

    Hermes' Desktop plugin REST bridge only carries JSON, so the pane asks for the data URL.
    """
    if encoding is None or encoding == "":
        return Response(data, media_type="image/jpeg", headers=headers)
    if encoding != "data_url":
        return _fail(400, "encoding must be 'data_url' when given")
    if len(data) > MAX_DATA_URL_BYTES:
        return _fail(413, "image too large to inline")
    url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    return JSONResponse({"ok": True, "data_url": url}, headers=headers)


@router.get("/frame/{source_key:path}")
async def frame(source_key: str, encoding: str | None = None) -> Any:
    result = await _run(_frame, source_key)
    if isinstance(result, Response):
        return result
    return _image_response(result, encoding, _NO_STORE)


def _list_rules() -> list[dict[str, Any]]:
    sentinel = _sentinel()
    if sentinel is None:
        return []
    return [_rule_item(sentinel, spec) for spec in sentinel.store.list_rules()]


@router.get("/rules")
async def rules() -> Any:
    result = await _run(_list_rules)
    return result if isinstance(result, Response) else _json(result)


def _update_rule(rule_id: str, action: str) -> dict[str, Any]:
    _check_rule_id(rule_id)
    sentinel = _need()
    spec = _known_rule(sentinel, rule_id)
    if action == "delete":
        before = _rule_item(sentinel, spec)
        _tool(sentinel, "sentinel_update", {"rule_id": rule_id, "action": "delete"})
        return {"ok": True, "rule": before}
    _tool(sentinel, "sentinel_update", {"rule_id": rule_id, "action": action})
    return {"ok": True, "rule": _rule_item(sentinel, _known_rule(sentinel, rule_id))}


@router.post("/rules/{rule_id}/pause")
async def pause_rule(rule_id: str) -> Any:
    result = await _run(_update_rule, rule_id, "pause")
    return result if isinstance(result, Response) else _json(result)


@router.post("/rules/{rule_id}/resume")
async def resume_rule(rule_id: str) -> Any:
    result = await _run(_update_rule, rule_id, "resume")
    return result if isinstance(result, Response) else _json(result)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str) -> Any:
    result = await _run(_update_rule, rule_id, "delete")
    return result if isinstance(result, Response) else _json(result)


def _list_events(limit: str | None, rule_id: str | None, since: str | None) -> list[dict]:
    if limit in (None, ""):
        n = DEFAULT_LIMIT
    elif _INT_RE.match(limit) and int(limit) >= 1:
        n = min(int(limit), MAX_LIMIT)
    else:
        raise ApiError(400, "limit must be a positive integer")
    rid = _check_rule_id(rule_id) if rule_id else None
    bound = None
    if since:
        if len(since) > 64:
            raise ApiError(400, "invalid 'since'")
        try:
            bound = _mod("tools").parse_time_bound(since)
        except Exception as exc:  # noqa: BLE001 - ToolError
            raise ApiError(400, _mask(str(exc))) from None
    sentinel = _sentinel()
    if sentinel is None:
        return []
    store = sentinel.store
    events = store.query_events(
        rule_id=rid, since=bound.astimezone(UTC) if bound else None, limit=n)
    rule_map = {spec.id: spec for spec in store.list_rules()}
    return [_event_item(e, rule_map) for e in events]


@router.get("/events")
async def events(limit: str | None = None, rule_id: str | None = None,
                 since: str | None = None) -> Any:
    result = await _run(_list_events, limit, rule_id, since)
    return result if isinstance(result, Response) else _json(result)


def _snapshot(event_id: str) -> bytes:
    eid = _check_event_id(event_id)
    sentinel = _need()
    store = sentinel.store
    event = store.get_event(eid)
    if event is None:
        raise ApiError(404, f"no event #{eid}")
    if not event.snapshot_path:
        raise ApiError(404, "this event has no snapshot")
    path = Path(event.snapshot_path).resolve()
    root = Path(store.data_dir).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ApiError(404, "the snapshot is gone")
    try:
        return path.read_bytes()
    except OSError:
        raise ApiError(404, "the snapshot is gone") from None


@router.get("/events/{event_id}/snapshot")
async def snapshot(event_id: str, encoding: str | None = None) -> Any:
    result = await _run(_snapshot, event_id)
    if isinstance(result, Response):
        return result
    return _image_response(result, encoding, {"Cache-Control": "private, max-age=3600"})


def _feedback(event_id: str, data: dict[str, Any]) -> dict[str, Any]:
    eid = _check_event_id(event_id)
    verdict = data.get("verdict")
    if verdict not in _VERDICTS:
        raise ApiError(400, "verdict must be 'correct' or 'false_alarm'")
    sentinel = _need()
    result = _tool(sentinel, "sentinel_feedback", {"event_id": eid, "verdict": verdict})
    store = sentinel.store
    event = store.get_event(eid)
    if event is None:
        raise ApiError(404, f"no event #{eid}")
    rule = store.get_rule(event.rule_id)
    threshold = result.get("confidence_threshold")
    if threshold is None and rule is not None:
        threshold = rule.confidence_threshold
    rules_map = {rule.id: rule} if rule is not None else {}
    return {"ok": True, "event": _event_item(event, rules_map),
            "threshold": float(threshold) if threshold is not None else 0.0}


@router.post("/events/{event_id}/feedback")
async def feedback(event_id: str, request: Request) -> Any:
    try:
        data = await _body(request)
    except ApiError as exc:
        return _fail(exc.status, exc.message)
    result = await _run(_feedback, event_id, data)
    return result if isinstance(result, Response) else _json(result)


def _watch(data: dict[str, Any]) -> dict[str, Any]:
    text = _opt_str(data, "text", MAX_TEXT)
    source = _opt_str(data, "source", MAX_SOURCE)
    recipe = _opt_str(data, "recipe", 64)
    if not text and not recipe:
        raise ApiError(400, "say what to watch for in 'text' (or pick a 'recipe')")
    sentinel = _need()
    args: dict[str, Any] = {}
    if text:
        args["what"] = text
    if source:
        args["source"] = source
    if recipe:
        args["recipe"] = recipe
    result = _tool(sentinel, "sentinel_watch", args)
    saved = _known_rule(sentinel, result["rule"]["rule_id"])
    out: dict[str, Any] = {"ok": True, "rule": _rule_item(sentinel, saved)}
    note = str(result.get("message") or "").split(" Alerts are injected")[0].strip()
    if note:
        out["note"] = _mask(note)
    return out


@router.post("/watch")
async def watch(request: Request) -> Any:
    try:
        data = await _body(request)
    except ApiError as exc:
        return _fail(exc.status, exc.message)
    result = await _run(_watch, data)
    return result if isinstance(result, Response) else _json(result)
