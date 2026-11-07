"""The dashboard router: every area's routes in one flat router."""

from __future__ import annotations

from fastapi import APIRouter

from .routes import events, images, rules, setup, sources, status

__all__ = ["build_router", "router"]

_AREAS = (status, sources, setup, images, rules, events)


def build_router() -> APIRouter:
    """One router holding every area's routes, in a fixed order.

    The route objects are copied, not nested with ``include_router``, so ``router.routes``
    stays a flat list on every FastAPI version (tools that inspect it keep working).
    """
    composed = APIRouter()
    for area in _AREAS:
        composed.routes.extend(area.router.routes)
    return composed


router = build_router()
