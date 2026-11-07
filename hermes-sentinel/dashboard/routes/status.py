"""``GET /status``: engine, delivery, budget and counts at a glance."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..common import access
from ..common.http import json_reply, run_blocking
from ..common.masking import mask
from ..common.shapes import iso
from ..common.source_listing import collect_sources

router = APIRouter()

__all__ = ["router"]


def _empty_status() -> dict[str, Any]:
    return {
        "engine": {"running": False, "standby": False, "since": None},
        "delivery": {"blocked": False, "reason": None},
        "budget": {
            "hour_used": 0,
            "hour_cap": None,
            "day_used": 0,
            "day_cap": None,
            "paused": False,
        },
        "sources": 0,
        "rules": 0,
        "version": access.version(),
    }


def _fill_from_budget(base: dict[str, Any], live: Any) -> None:
    st = live.budget.status()
    base["budget"] = {
        "hour_used": st.hour_used,
        "hour_cap": st.hour_cap,
        "day_used": st.day_used,
        "day_cap": st.day_cap,
        "paused": bool(st.exhausted or st.hour_cap == 0),
    }
    base["rules"] = len(live.store.list_rules())
    base["sources"] = len(collect_sources(live))


def build_status() -> dict[str, Any]:
    """The status payload; degrades to zeros and a note when Sentinel or storage is missing."""
    live = access.sentinel()
    base = _empty_status()
    if live is None:
        base["note"] = access.NOT_LOADED
        return base
    engine = live.engine_if_built
    if engine is not None:
        base["engine"] = {
            "running": bool(engine.running),
            "standby": bool(engine.standing_by),
            "since": iso(engine.started_at) if engine.running else None,
        }
    reason = live.alerter.delivery_blocked()
    base["delivery"] = {"blocked": bool(reason), "reason": reason}
    cfg = live.config
    base["budget"].update(
        hour_cap=cfg.vision_calls_per_hour,
        day_cap=cfg.vision_calls_per_day,
        paused=cfg.vision_calls_per_hour == 0,
    )
    try:
        _fill_from_budget(base, live)
    except Exception as exc:  # noqa: BLE001 - storage down: report what we know
        base["note"] = mask(f"storage unavailable: {type(exc).__name__}")
    return base


@router.get("/status")
async def status() -> Any:
    """The status payload (never fails: a missing plugin is reported in ``note``)."""
    return json_reply(await run_blocking(build_status))
