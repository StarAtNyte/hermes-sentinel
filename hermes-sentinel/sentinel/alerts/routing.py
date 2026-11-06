"""Origin and route helpers: where an alert for a rule should be delivered."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .route_book import RouteBook

#: Session surfaces that are not a messaging chat (an ``origin`` route makes no sense there).
LOCAL_SURFACES = frozenset({"", "cli", "tui", "desktop", "local", "acp", "api_server", "gateway"})
#: Origin keys forwarded to ``inject_message(origin=...)`` (``SessionSource.to_dict`` shape).
ROUTE_KEYS = ("platform", "chat_id", "chat_type", "chat_name", "thread_id", "user_id",
              "user_name")
MAX_ORIGIN_VALUE = 500


def clean_value(value: Any) -> str | None:
    """A non-empty, bounded scalar as text, or ``None`` for anything else."""
    value = getattr(value, "value", value)  # enums (e.g. a Platform) -> their value
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_ORIGIN_VALUE:
        return None
    return text


def _from_source(source: Any, key: str) -> str | None:
    raw = source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
    return clean_value(raw)


def route_from_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, str]:
    """Routing fields from handler keywords (``session_key``, ``platform``, ``source``...)."""
    kwargs = kwargs or {}
    out: dict[str, str] = {}
    source = kwargs.get("source")
    for key in ("session_key", *ROUTE_KEYS):
        value = clean_value(kwargs.get(key))
        if value is None and source is not None and key in ROUTE_KEYS:
            value = _from_source(source, key)
        if value is not None:
            out[key] = value
    return out


def capture_origin(
    kwargs: Mapping[str, Any] | None,
    ctx: Any = None,
    *,
    routes: RouteBook | None = None,
) -> dict[str, Any]:
    """Where alerts for a rule created in this call should go.

    Reads the handler keywords (``session_id``, plus ``session_key`` / ``platform`` /
    ``source`` when the host passes them), ``ctx.session_surface()``, and a route remembered
    for this ``session_id`` by ``/sentinel``. Returns only non-empty scalar values (a valid
    ``RuleSpec.origin``). Never raises.
    """
    kwargs = kwargs or {}
    out: dict[str, Any] = dict(route_from_kwargs(kwargs))
    session_id = clean_value(kwargs.get("session_id"))
    if session_id:
        out["session_id"] = session_id
    if ctx is not None:
        try:
            surface = clean_value(ctx.session_surface())
        except Exception:  # noqa: BLE001
            surface = None
        if surface:
            out["surface"] = surface
    if routes is not None and session_id and "session_key" not in out:
        for key, value in routes.lookup(session_id).items():
            out.setdefault(key, value)
    return out


def is_chat_origin(origin: Mapping[str, Any] | None) -> bool:
    """True when ``origin`` names a messaging platform (anything but a local surface)."""
    origin = origin or {}
    for key in ("platform", "surface"):
        value = (clean_value(origin.get(key)) or "").lower()
        if value and value not in LOCAL_SURFACES:
            return True
    return False


def same_chat(rule_origin: Mapping[str, Any] | None, caller: Mapping[str, Any] | None) -> bool:
    """Whether a rule created at ``rule_origin`` belongs to the chat described by ``caller``.

    Local callers (CLI, TUI, Desktop) own everything. A messaging caller owns rules with the
    same session key, the same platform and chat, or the same session id.
    """
    if not is_chat_origin(caller):
        return True
    rule_origin, caller = rule_origin or {}, caller or {}
    for key in ("session_key", "session_id"):
        a, b = clean_value(rule_origin.get(key)), clean_value(caller.get(key))
        if a and a == b:
            return True
    platform = (clean_value(rule_origin.get("platform")) or "").lower()
    chat = clean_value(rule_origin.get("chat_id"))
    return bool(
        platform and chat
        and platform == (clean_value(caller.get("platform")) or "").lower()
        and chat == clean_value(caller.get("chat_id"))
    )
