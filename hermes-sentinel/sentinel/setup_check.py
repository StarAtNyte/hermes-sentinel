"""One-command setup check: what is missing before Sentinel can watch and alert?

``run_checks(env)`` is pure and testable: every input (importability, camera probe, disk,
storage, engine, route book) is a field of :class:`SetupEnv`, so tests pass fakes. Each check
is wrapped so it never raises, never touches the network and is bounded in time (the device
probe reuses :func:`sources.list_sources` with short timeouts).

Surfaces: ``/sentinel setup`` (commands.py), the ``sentinel_setup_check`` tool (tools.py), and
``GET /setup`` on the dashboard API. Public plugin APIs only. A plugin cannot read its own
``plugins.entries.<id>.allow_gateway_injection`` flag or ``auxiliary.*`` routing, so those two
checks say what they can and never claim more.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .alerts import GATEWAY_INJECTION_YAML
from .sources import (
    SourceSpec,
    SourceSpecError,
    list_sources,
    probe_secrets,
    redact_url,
    referenced_secrets,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "SetupEnv",
    "fail_titles",
    "format_report",
    "run_checks",
    "summarize",
]

OK, WARN, FAIL = "ok", "warn", "fail"

DEPS_HINT = (
    "Install the plugin dependencies numpy>=1.26,<3, opencv-python-headless>=4.8,<6 and "
    "mss>=9,<11: enabling the plugin (hermes plugins enable hermes-sentinel) installs them for you."
)
VISION_YAML = (
    "auxiliary:\n  sentinel_vision:\n    provider: <your provider>\n"
    "    model: <a vision-capable model>"
)
LOW_DISK_WARN_MB = 500
LOW_DISK_FAIL_MB = 50
PROBE_TOTAL_TIMEOUT = 4.0
PROBE_CAMERA_TIMEOUT = 1.5
HOURLY_SANE_MAX = 600


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: str  # ok | warn | fail
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _probe(**kwargs: Any) -> list[Any]:
    return list_sources(**kwargs)


@dataclass
class SetupEnv:
    """Everything the checks look at. Defaults are cheap; ``from_sentinel`` fills real ones."""

    config: Any = None  # SentinelConfig
    has_llm: bool | None = None  # ctx.llm present?
    store_getter: Callable[[], Any] | None = None  # -> Store (may raise)
    engine: Any = None  # Engine or None when never built
    alerter: Any = None
    budget_getter: Callable[[], Any] | None = None
    route: Mapping[str, Any] | None = None  # None: not applicable (no session)
    probe_sources: bool = True
    module_available: Callable[[str], bool] = _find_spec
    list_sources: Callable[..., list[Any]] = _probe
    disk_free_mb: Callable[[Any], float] = field(
        default=lambda path: shutil.disk_usage(path).free / (1024 * 1024)
    )

    @classmethod
    def from_sentinel(
        cls, sentinel: Any, kwargs: Mapping[str, Any] | None = None, *, probe_sources: bool = True
    ) -> SetupEnv:
        """Build from a live :class:`Sentinel`. ``kwargs``: the calling session, if any."""
        route: dict[str, Any] | None = None
        if kwargs is not None:
            try:
                from .alerts import route_from_kwargs

                route = route_from_kwargs(kwargs)
                if not route.get("session_key"):
                    route = sentinel.routes.lookup(str(kwargs.get("session_id") or "")) or route
            except Exception:  # noqa: BLE001
                route = {}
        engine = sentinel.engine_if_built
        if engine is not None and getattr(engine, "running", False):
            probe_sources = False  # never fight the watcher for a camera
        return cls(
            config=sentinel.config,
            has_llm=getattr(sentinel.ctx, "llm", None) is not None,
            store_getter=lambda: sentinel.store,
            engine=engine,
            alerter=sentinel.alerter,
            budget_getter=lambda: sentinel.budget,
            route=route,
            probe_sources=probe_sources,
        )


# --------------------------------------------------------------------------- helpers


def _mask(text: Any) -> str:
    """Strip credentials from any URL-looking word."""
    out = []
    for word in str(text or "").split(" "):
        if "://" in word:
            try:
                word = redact_url(word)
            except Exception:  # noqa: BLE001
                word = "<url>"
        out.append(word)
    return " ".join(out)[:600]


def _named_streams(env: SetupEnv) -> dict[str, str]:
    return dict(getattr(env.config, "stream_urls", None) or {})


# --------------------------------------------------------------------------- checks


def _check_deps(env: SetupEnv) -> Check:
    missing = [m for m in ("numpy", "cv2", "mss") if not env.module_available(m)]
    if not missing:
        return Check("deps", "Python packages", OK, "numpy, opencv and mss are installed.")
    names = {"cv2": "opencv-python-headless"}
    pretty = ", ".join(names.get(m, m) for m in missing)
    if set(missing) == {"mss"}:
        return Check(
            "deps",
            "Python packages",
            WARN,
            "mss is missing, so screen capture will not work (cameras are fine).",
            DEPS_HINT,
        )
    return Check(
        "deps",
        "Python packages",
        FAIL,
        f"Missing: {pretty}.",
        DEPS_HINT + "  (in the Python environment Hermes runs in)",
    )


def _delivery_verified(alerter: Any) -> bool:
    """Whether a delivery self-test succeeded (`/sentinel setup --test-delivery`)."""
    check = getattr(alerter, "delivery_verified", None)
    return bool(check()) if callable(check) else False


def _check_injection(env: SetupEnv) -> Check:
    title = "Alert delivery permission"
    blocked = env.alerter.delivery_blocked() if env.alerter is not None else None
    if blocked:
        return Check(
            "injection",
            title,
            FAIL,
            "Hermes refused to inject Sentinel's alerts into the chat.",
            GATEWAY_INJECTION_YAML,
        )
    if _delivery_verified(env.alerter):
        return Check("injection", title, OK, "The delivery test message was accepted by Hermes.")
    sent = 0
    try:
        sent = int((env.alerter.stats() if env.alerter is not None else {}).get("sent", 0))
    except Exception:  # noqa: BLE001
        sent = 0
    if sent > 0:
        return Check("injection", title, OK, f"{sent} alert(s) delivered since start.")
    return Check(
        "injection",
        title,
        WARN,
        "Cannot be verified from inside a plugin. The classic CLI needs nothing; "
        "every other surface (Desktop, Telegram, Discord...) needs this opt-in "
        "(off by default), otherwise alerts are refused. "
        "Run /sentinel setup --test-delivery to try it (starts one agent turn).",
        GATEWAY_INJECTION_YAML,
    )


def _check_vision(env: SetupEnv) -> Check:
    title = "Vision model"
    if env.has_llm is False:
        return Check(
            "vision",
            title,
            FAIL,
            "This Hermes did not give the plugin an LLM interface (ctx.llm).",
            "Update Hermes to a release that supports plugin LLM calls (>=0.22.5).",
        )
    return Check(
        "vision",
        title,
        WARN,
        "Plugins cannot read model routing, so this is unverified: unless you set "
        "the auxiliary task sentinel_vision, it uses your default model, which must "
        "accept images.",
        VISION_YAML,
    )


def _check_sources(env: SetupEnv) -> tuple[Check, list[Any]]:
    title = "Cameras and screens"
    if not env.probe_sources:
        return Check(
            "sources",
            title,
            OK,
            "Devices not probed here (skipped, or the watcher holds them). Run "
            "/sentinel setup or press Check devices.",
        ), []
    infos = env.list_sources(
        named_streams=_named_streams(env),
        total_timeout=PROBE_TOTAL_TIMEOUT,
        per_camera_timeout=PROBE_CAMERA_TIMEOUT,
    )
    found = [i for i in infos if i.available is True]
    configured = [i for i in infos if i.available is None]
    if found or configured:
        parts = [f"{i.spec} ({i.name})" for i in found] + [f"{i.spec} (stream)" for i in configured]
        return Check("sources", title, OK, "Found: " + ", ".join(parts) + "."), infos
    return Check(
        "sources",
        title,
        WARN,
        "No camera, monitor or stream found (a headless box, no webcam, or a permission problem).",
        "Plug in or allow a camera, or watch a snapshot:https://..., rtsp:// or "
        "folder:/path source instead.",
    ), infos


def _check_secrets(env: SetupEnv) -> Check | None:
    """Can every ``${SECRET}`` a stream or rule references be read? ``None`` if none are used."""
    title = "Camera secrets"
    texts = list(_named_streams(env).values())
    if env.store_getter is not None:
        texts += [rule.source for rule in env.store_getter().list_rules(enabled_only=True)]
    status = probe_secrets(referenced_secrets(texts))
    if not status.referenced:
        return None
    if status.missing:
        names = ", ".join(status.missing)
        return Check(
            "secrets",
            title,
            WARN,
            f"Not set: {names}. Cameras that use them stay offline.",
            "Set them in Hermes (plugin settings for hermes-sentinel, or the profile .env).",
        )
    if status.waiting:
        names = ", ".join(status.waiting)
        return Check(
            "secrets",
            title,
            WARN,
            f"Waiting for the Hermes secret scope: {names}. This multiplexed gateway only "
            "exposes secrets inside a tool or command call.",
            "Run /sentinel status or any sentinel tool once; the values are then kept in memory "
            "only and the cameras connect by themselves.",
        )
    return Check("secrets", title, OK, f"{len(status.ready)} secret(s) readable, none shown.")


def _check_default_source(env: SetupEnv, infos: list[Any]) -> Check:
    title = "Default source"
    raw = getattr(env.config, "default_source", "camera:0")
    try:
        spec = SourceSpec.parse(raw, named_streams=_named_streams(env))
    except SourceSpecError as exc:
        return Check(
            "default_source",
            title,
            FAIL,
            f"default_source {_mask(raw)!r} is not valid: {_mask(exc)}",
            "Set default_source to camera:N, screen[:N], rtsp://..., stream:<name>, "
            "snapshot:https://... or folder:/path in the plugin settings.",
        )
    label = _mask(spec.name or raw)
    if spec.kind in ("camera", "screen") and infos:
        want = f"{spec.kind}:{spec.target}"
        known = {i.spec for i in infos if i.available is True}
        if spec.kind == "screen" and spec.target == 0:
            known |= {"screen:0"}
        if known and want not in known:
            return Check(
                "default_source",
                title,
                WARN,
                f"{want} was not found; available: {', '.join(sorted(known))}.",
                "Change default_source to one of those.",
            )
    return Check("default_source", title, OK, f"{label} parses as a {spec.kind} source.")


def _check_config(env: SetupEnv) -> Check | None:
    problems = list(getattr(env.config, "problems", ()) or ())
    if not problems:
        return None
    return Check(
        "config",
        "Plugin settings",
        WARN,
        _mask("; ".join(problems)),
        "Fix the listed values under plugins.entries.hermes-sentinel.settings.",
    )


def _check_engine(env: SetupEnv) -> Check:
    title = "Watcher"
    engine = env.engine
    if engine is not None and getattr(engine, "running", False):
        return Check("engine", title, OK, "Running.")
    if engine is not None and getattr(engine, "standing_by", False):
        return Check("engine", title, OK, "Standing by: another Hermes process is watching.")
    return Check(
        "engine",
        title,
        OK,
        "Not started yet; it starts with the first watch rule (or shortly after Hermes "
        "loads when enabled rules exist).",
    )


def _check_storage(env: SetupEnv) -> Check:
    title = "Storage"
    if env.store_getter is None:
        return Check("storage", title, WARN, "Storage was not inspected.")
    try:
        store = env.store_getter()
        path = store.data_dir
        probe = path / ".setup-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "storage",
            title,
            FAIL,
            f"Cannot open or write the data directory: {_mask(exc)}",
            "Check permissions and free space of HERMES_HOME/plugin-data/hermes-sentinel.",
        )
    try:
        free = float(env.disk_free_mb(path))
    except Exception:  # noqa: BLE001
        return Check("storage", title, OK, f"Writable at {path}.")
    if free < LOW_DISK_FAIL_MB:
        return Check(
            "storage",
            title,
            FAIL,
            f"Only {free:.0f} MB free at {path}.",
            "Free disk space; snapshots cannot be saved.",
        )
    if free < LOW_DISK_WARN_MB:
        return Check(
            "storage",
            title,
            WARN,
            f"Writable, but only {free:.0f} MB free.",
            "Free disk space or lower max_storage_mb.",
        )
    return Check("storage", title, OK, f"Writable at {path}, {free / 1024:.1f} GB free.")


def _check_delivery(env: SetupEnv) -> Check:
    title = "Alert destination"
    if env.route is None:
        return Check(
            "delivery",
            title,
            OK,
            "Not tied to a session here. Run /sentinel setup in the chat that should "
            "get alerts to check it.",
        )
    if env.route.get("session_key"):
        return Check("delivery", title, OK, "This chat is known; alerts will come back here.")
    return Check(
        "delivery",
        title,
        WARN,
        "This session did not identify its chat, so alerts may not find their way back.",
        "Send /sentinel here in the chat that should receive alerts.",
    )


def _check_budget(env: SetupEnv) -> Check:
    title = "Vision call caps"
    cfg = env.config
    hour = getattr(cfg, "vision_calls_per_hour", None)
    day = getattr(cfg, "vision_calls_per_day", None)
    if hour == 0 or day == 0:
        return Check(
            "budget",
            title,
            FAIL,
            "A cap of 0 pauses all judging; nothing can fire.",
            "Set vision_calls_per_hour / vision_calls_per_day above 0.",
        )
    if hour is None or day is None:
        return Check(
            "budget",
            title,
            WARN,
            "No cap on vision calls: a busy scene can cost a lot.",
            "Set vision_calls_per_hour (default 60) and vision_calls_per_day (500).",
        )
    if day < hour:
        return Check(
            "budget",
            title,
            WARN,
            f"Daily cap {day} is below the hourly cap {hour}; the daily cap will bind.",
            "Raise vision_calls_per_day or lower vision_calls_per_hour.",
        )
    if hour > HOURLY_SANE_MAX:
        return Check(
            "budget",
            title,
            WARN,
            f"{hour}/hour is a high cap for a cloud model.",
            "Lower vision_calls_per_hour unless a local model does the judging.",
        )
    return Check("budget", title, OK, f"{hour}/hour, {day}/day.")


def _check_rules(env: SetupEnv) -> Check:
    title = "Rules"
    if env.store_getter is None:
        return Check("rules", title, WARN, "Rules were not inspected.")
    rules = env.store_getter().list_rules()
    if not rules:
        return Check("rules", title, OK, "No rules yet. Ask Hermes to watch something.")
    enabled = [r for r in rules if r.enabled]
    detail = f"{len(rules)} rule(s), {len(enabled)} active."
    offline: list[str] = []
    engine = env.engine
    if engine is not None and getattr(engine, "running", False):
        for src in engine.status().get("sources", []):
            if src.get("online") is False:
                label = _mask(src.get("source"))
                offline.extend(f"{rid} ({label})" for rid in src.get("rules", []))
    if offline:
        return Check(
            "rules",
            title,
            WARN,
            f"{detail} Source offline for: {', '.join(offline)}.",
            "Check the camera/stream is connected and not used by another program.",
        )
    if (
        enabled
        and engine is not None
        and not getattr(engine, "running", False)
        and not getattr(engine, "standing_by", False)
    ):
        return Check(
            "rules",
            title,
            WARN,
            f"{detail} The watcher is not running.",
            "It starts shortly after Hermes loads; use /sentinel status to confirm.",
        )
    return Check("rules", title, OK, detail)


# --------------------------------------------------------------------------- driver


def _safe(check_id: str, title: str, fn: Callable[[], Check | None]) -> Check | None:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a check must report, never raise
        logger.debug("sentinel: setup check %s failed", check_id, exc_info=True)
        return Check(check_id, title, WARN, f"Could not be checked ({type(exc).__name__}).")


def run_checks(env: SetupEnv) -> list[Check]:
    """Run every check in a fixed, useful order. Never raises, never touches the network."""
    infos: list[Any] = []
    out: list[Check | None] = [_safe("deps", "Python packages", lambda: _check_deps(env))]

    def sources() -> Check:
        nonlocal infos
        check, infos = _check_sources(env)
        return check

    out.append(_safe("injection", "Alert delivery permission", lambda: _check_injection(env)))
    out.append(_safe("vision", "Vision model", lambda: _check_vision(env)))
    out.append(_safe("sources", "Cameras and screens", sources))
    out.append(_safe("default_source", "Default source", lambda: _check_default_source(env, infos)))
    out.append(_safe("secrets", "Camera secrets", lambda: _check_secrets(env)))
    out.append(_safe("config", "Plugin settings", lambda: _check_config(env)))
    out.append(_safe("engine", "Watcher", lambda: _check_engine(env)))
    out.append(_safe("storage", "Storage", lambda: _check_storage(env)))
    out.append(_safe("delivery", "Alert destination", lambda: _check_delivery(env)))
    out.append(_safe("budget", "Vision call caps", lambda: _check_budget(env)))
    out.append(_safe("rules", "Rules", lambda: _check_rules(env)))
    return [c for c in out if c is not None]


def summarize(checks: list[Check]) -> dict[str, Any]:
    """``ready`` is true when nothing failed; ``to_fix`` counts every non-ok check."""
    failed = sum(1 for c in checks if c.status == FAIL)
    to_fix = sum(1 for c in checks if c.status != OK)
    return {"ready": failed == 0, "failed": failed, "to_fix": to_fix}


def fail_titles(checks: list[Check]) -> list[str]:
    return [c.title for c in checks if c.status == FAIL]


def format_report(checks: list[Check]) -> str:
    marks = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = ["Sentinel setup check"]
    for c in checks:
        lines.append(f"{marks.get(c.status, '[?]   ')} {c.title}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append("       fix: " + c.fix.replace("\n", "\n            "))
    s = summarize(checks)
    noun = "thing" if s["to_fix"] == 1 else "things"
    lines.append(f"Ready: {'yes' if s['ready'] else 'no'} - {s['to_fix']} {noun} to fix")
    return "\n".join(lines)
