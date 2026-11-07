"""``sentinel_sources``: list the cameras, monitors and streams that can be watched."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....sources import list_sources
from ..common import ToolContext, ToolSpec, ok, schema

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_sources",
    "List the cameras, monitors and configured streams Sentinel can watch. Probing local "
    "cameras takes a few seconds; set probe_cameras=false to skip it.",
    {"probe_cameras": {"type": "boolean", "description": "Probe webcams 0-4 (default true)."}},
)


def run(context: ToolContext, args: dict[str, Any], _kwargs: Mapping[str, Any]) -> str:
    """The available sources, probing local webcams unless ``probe_cameras`` is false."""
    config = context.sentinel.config
    probe = args.get("probe_cameras", True)
    infos = list_sources(named_streams=config.stream_urls, probe_cameras=bool(probe))
    return ok(
        sources=[i.to_dict() for i in infos],
        default_source=config.default_source,
        hint="Also possible: 'snapshot:https://host/cam.jpg', 'folder:/path/to/images', "
        "'screen:1@x,y,w,h' for part of a screen.",
    )


SPEC = ToolSpec("sentinel_sources", SCHEMA, "\U0001f4f7", run)
