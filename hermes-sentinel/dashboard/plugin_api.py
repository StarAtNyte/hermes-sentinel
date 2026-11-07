"""Sentinel dashboard / Desktop backend, mounted by Hermes at /api/plugins/hermes-sentinel/.

Hermes imports this file on its own, not as part of the plugin package, so it only finds the
plugin package Hermes already loaded (a read-only scan of ``sys.modules``) and exposes the
router built in ``sentinel/dashboard_api/``. If the plugin is not loaded in this process every
route answers 503, so a dashboard started without the agent process degrades instead of
crashing at import.
"""

from __future__ import annotations

import importlib
import sys

from fastapi import APIRouter
from fastapi.responses import JSONResponse

PACKAGE = "hermes_plugins.hermes_sentinel"
SCOPED_PREFIX = PACKAGE + "__home_"  # Hermes names a second-scope plugin ...__home_<digest>
NOT_LOADED = (
    "Sentinel is not loaded in this process: the Desktop pane must be served by the same "
    "Hermes process that runs the agent and loaded the hermes-sentinel plugin"
)
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _is_live(name: str) -> bool:
    try:
        runtime = importlib.import_module(f"{name}.sentinel.runtime")
        return runtime.active_sentinel() is not None
    except (ImportError, AttributeError):
        return False


def find_package() -> str | None:
    """Name of the loaded plugin package, preferring the one with a live Sentinel."""
    names = sorted(k for k in list(sys.modules) if k == PACKAGE or k.startswith(SCOPED_PREFIX))
    return next((n for n in names if _is_live(n)), names[0] if names else None)


def _not_loaded_router() -> APIRouter:
    fallback = APIRouter()

    @fallback.api_route("/{path:path}", methods=METHODS)
    async def not_loaded(path: str) -> JSONResponse:
        """Every route answers 503 while the plugin is not loaded here."""
        return JSONResponse({"ok": False, "error": NOT_LOADED}, status_code=503)

    return fallback


def build_router() -> APIRouter:
    """The plugin's dashboard router, or the 503 fallback when the plugin is not loaded."""
    name = find_package()
    if name is None:
        return _not_loaded_router()
    return importlib.import_module(f"{name}.sentinel.dashboard_api.router").router


router = build_router()
