"""Sentinel dashboard / Desktop backend, mounted by Hermes at /api/plugins/hermes-sentinel/.

A thin HTTP view over the *same* runtime the agent tools use (``Sentinel``: store, budget,
engine, ``SentinelTools``). It adds no rule logic of its own: pause / resume / delete, feedback
and watch all go through ``SentinelTools`` handlers; reads come from the store and the engine's
in-memory capture state.

Hermes imports this file on its own (not as part of the plugin package), so it composes the
routers of its sibling ``routes/`` package by registering the dashboard directory as a package
of its own. The plugin modules are reached through the package name Hermes' loader gives the
plugin (``common/access.py``). If the plugin is not loaded in this process every read degrades
to an empty answer and every write returns 503.

Rules of the road: dashboard callers are the local owner (no messaging-origin scoping);
responses never carry credentials (source specs are redacted); request bodies and limits are
bounded; no handler touches a camera (frames come from the capture thread's latest frame) and
blocking work runs in worker threads.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

from fastapi import APIRouter

AREAS = ("status", "sources", "setup", "images", "rules", "events")


def _register_package(name: str) -> None:
    """Make this directory importable as package ``name`` (Hermes loads this file standalone)."""
    if name in sys.modules:
        return
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[name] = package  # rule-lint: ignore[9] own dashboard directory, not Hermes core


def _compose(package_name: str) -> APIRouter:
    """One router holding every area's routes, in a fixed order.

    The route objects are copied, not nested with ``include_router``, so ``router.routes``
    stays a flat list on every FastAPI version (tools that inspect it keep working).
    """
    composed = APIRouter()
    for area in AREAS:
        area_router = importlib.import_module(f"{package_name}.routes.{area}").router
        composed.routes.extend(area_router.routes)
    return composed


PACKAGE = f"{__name__}__api"
_register_package(PACKAGE)
router = _compose(PACKAGE)
