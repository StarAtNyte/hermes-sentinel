"""``GET /sources``: the sources rules use, with live online state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..common import access
from ..common.http import reply, run_blocking
from ..common.source_listing import collect_sources

router = APIRouter()

__all__ = ["router"]


def _list_sources() -> list[dict[str, Any]]:
    live = access.sentinel()
    return collect_sources(live) if live is not None else []


@router.get("/sources")
async def sources() -> Any:
    """Sources in use by rules (empty when the plugin is not loaded here)."""
    return reply(await run_blocking(_list_sources))
