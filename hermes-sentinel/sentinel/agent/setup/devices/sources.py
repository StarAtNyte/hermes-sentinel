"""Probe the cameras, monitors and streams Sentinel could watch."""

from __future__ import annotations

from typing import Any

from ..env import SetupEnv
from ..masking import named_streams
from ..model import OK, WARN, Check

__all__ = ["check_sources"]

PROBE_TOTAL_TIMEOUT = 4.0
PROBE_CAMERA_TIMEOUT = 1.5


def check_sources(env: SetupEnv) -> tuple[Check, list[Any]]:
    """The check plus the probed source infos (empty when probing was skipped)."""
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
        named_streams=named_streams(env),
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
