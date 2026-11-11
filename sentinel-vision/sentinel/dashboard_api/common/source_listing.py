"""The sources in use, merged from the stored rules and the capture workers' live state."""

from __future__ import annotations

from typing import Any

from .masking import mask, source_key, source_label_kind

__all__ = ["collect_sources"]


def _offline_reason(engine: Any, running: bool) -> str | None:
    if running:
        return None
    if engine is not None and engine.standing_by:
        return "another Hermes process is running the watcher"
    return "watcher is not running"


def _group_by_key(live: Any, infos: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for spec in live.store.list_rules():
        group = groups.setdefault(source_key(live, spec.source), {"spec": spec.source, "rules": 0})
        group["rules"] += 1
    for key in infos:
        groups.setdefault(mask(key), {"spec": key, "rules": len(infos[key]["rules"])})
    return groups


def collect_sources(live: Any) -> list[dict[str, Any]]:
    """Sources in use by rules, merged with the capture workers' in-memory state."""
    engine = live.engine_if_built
    running = bool(engine is not None and engine.running)
    infos = engine.source_infos() if running else {}
    groups = _group_by_key(live, infos)
    internal = {mask(k): k for k in infos}
    out = []
    for key in sorted(groups):
        info = infos.get(internal.get(key, key))
        label, kind = source_label_kind(groups[key]["spec"], live.config.stream_urls)
        if info is not None:
            online, age, error = info["online"], info["last_frame_age_s"], info["error"]
        else:
            online, age, error = False, None, _offline_reason(engine, running)
        out.append(
            {
                "key": key,
                "label": label,
                "kind": kind,
                "online": online,
                "last_frame_age_s": age,
                "rules": groups[key]["rules"],
                "error": mask(error) if error else None,
            }
        )
    return out
