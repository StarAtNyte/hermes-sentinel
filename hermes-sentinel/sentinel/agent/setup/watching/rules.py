"""Summarise the watch rules and flag offline sources or a stopped watcher."""

from __future__ import annotations

from ..env import SetupEnv
from ..masking import mask
from ..model import OK, WARN, Check

__all__ = ["check_rules"]


def _offline_labels(env: SetupEnv) -> list[str]:
    """``rule (source)`` for every rule whose source the running watcher reports offline."""
    engine = env.engine
    if engine is None or not getattr(engine, "running", False):
        return []
    offline: list[str] = []
    for src in engine.status().get("sources", []):
        if src.get("online") is False:
            label = mask(src.get("source"))
            offline.extend(f"{rid} ({label})" for rid in src.get("rules", []))
    return offline


def check_rules(env: SetupEnv) -> Check:
    """OK with a count; WARN when sources are offline or enabled rules have no watcher."""
    title = "Rules"
    if env.store_getter is None:
        return Check("rules", title, WARN, "Rules were not inspected.")
    rules = env.store_getter().list_rules()
    if not rules:
        return Check("rules", title, OK, "No rules yet. Ask Hermes to watch something.")
    enabled = [r for r in rules if r.enabled]
    detail = f"{len(rules)} rule(s), {len(enabled)} active."
    offline = _offline_labels(env)
    if offline:
        return Check(
            "rules",
            title,
            WARN,
            f"{detail} Source offline for: {', '.join(offline)}.",
            "Check the camera/stream is connected and not used by another program.",
        )
    engine = env.engine
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
