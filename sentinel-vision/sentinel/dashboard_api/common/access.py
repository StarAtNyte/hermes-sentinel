"""Reach the loaded plugin from the dashboard process.

Hermes imports ``plugin_api.py`` on its own, not as part of the plugin package, so plugin
modules are reached through the package name Hermes' loader gave the plugin. If the plugin is
not loaded in this process every read degrades to an empty answer and every write returns 503.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from typing import Any

from .http import ApiError

logger = logging.getLogger(__name__)

__all__ = [
    "NOT_LOADED",
    "OWNER_ORIGIN",
    "active",
    "module",
    "need",
    "package",
    "package_names",
    "sentinel",
    "tools",
    "version",
]

PACKAGE = "hermes_plugins.sentinel_vision"
OWNER_ORIGIN = {"surface": "desktop"}  # the dashboard user owns every rule
NOT_LOADED = (
    "Sentinel is not loaded in this process: the Desktop pane must be served by the same "
    "Hermes process that runs the agent and loaded the sentinel-vision plugin"
)
_NAMESPACE_SUFFIX = "__home_"  # Hermes names a second-scope plugin sentinel_vision__home_<digest>
_MAX_CACHED_TOOLS = 8
_TOOLS: dict[int, Any] = {}
_TOOLS_LOCK = threading.Lock()  # guards _TOOLS


def package_names() -> list[str]:
    """Names of loaded Sentinel plugin packages (Hermes names vary by profile scope)."""
    return sorted(
        k for k in list(sys.modules) if k == PACKAGE or k.startswith(PACKAGE + _NAMESPACE_SUFFIX)
    )


def active() -> tuple[str, Any] | None:
    """``(package name, Sentinel)`` of the loaded plugin whose runtime has a live Sentinel."""
    for name in package_names():
        try:
            runtime = importlib.import_module(f"{name}.sentinel.runtime")
            live = runtime.active_sentinel()
        except Exception as exc:  # noqa: BLE001 - broad: a partially imported or foreign module
            logger.debug("sentinel: package %s has no live runtime: %s", name, type(exc).__name__)
            continue
        if live is not None:
            return name, live
    return None


def package() -> str:
    """The package name to import plugin modules from (the active one when there is one)."""
    found = active()
    if found is not None:
        return found[0]
    names = package_names()
    return names[0] if names else PACKAGE


def module(name: str) -> Any:
    """The plugin submodule ``sentinel.<name>`` of the active package."""
    return importlib.import_module(f"{package()}.sentinel.{name}")


def sentinel() -> Any | None:
    """The loaded :class:`Sentinel`, or ``None`` when the plugin is not loaded here."""
    found = active()
    return found[1] if found is not None else None


def need() -> Any:
    """The loaded Sentinel, or a 503 :class:`ApiError` when the plugin is not loaded here."""
    live = sentinel()
    if live is None:
        raise ApiError(503, NOT_LOADED)
    return live


def version() -> str:
    """The plugin version, ``"unknown"`` when it cannot be read."""
    try:
        return str(module("version").__version__)
    except Exception as exc:  # noqa: BLE001 - broad: module() may raise ApiError or ImportError
        logger.debug("sentinel: plugin version unreadable: %s", type(exc).__name__)
        return "unknown"


def tools(live: Any) -> Any:
    """One ``SentinelTools`` per Sentinel (keeps its compile-rate cap), fixed local owner."""
    with _TOOLS_LOCK:
        entry = _TOOLS.get(id(live))
        if entry is None or entry[0] is not live:
            if len(_TOOLS) > _MAX_CACHED_TOOLS:
                _TOOLS.clear()
            built = module("agent.tools").SentinelTools(live, owner_origin=OWNER_ORIGIN)
            entry = (live, built)
            _TOOLS[id(live)] = entry
        return entry[1]
