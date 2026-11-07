"""Run every setup check in a fixed order, each one isolated from the others' failures."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .devices import check_default_source, check_secrets, check_sources
from .env import SetupEnv
from .install import check_config, check_deps, check_storage, check_vision
from .model import WARN, Check
from .watching import (
    check_budget,
    check_delivery,
    check_engine,
    check_injection,
    check_rules,
)

logger = logging.getLogger(__name__)

__all__ = ["run_checks"]


def _safe(check_id: str, title: str, fn: Callable[[], Check | None]) -> Check | None:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a check must report, never raise
        logger.debug("sentinel: setup check %s failed", check_id, exc_info=True)
        return Check(check_id, title, WARN, f"Could not be checked ({type(exc).__name__}).")


def run_checks(env: SetupEnv) -> list[Check]:
    """Run every check in a fixed, useful order. Never raises, never touches the network."""
    infos: list[Any] = []
    out: list[Check | None] = [_safe("deps", "Python packages", lambda: check_deps(env))]

    def sources() -> Check:
        nonlocal infos
        check, infos = check_sources(env)
        return check

    out.append(_safe("injection", "Alert delivery permission", lambda: check_injection(env)))
    out.append(_safe("vision", "Vision model", lambda: check_vision(env)))
    out.append(_safe("sources", "Cameras and screens", sources))
    out.append(_safe("default_source", "Default source", lambda: check_default_source(env, infos)))
    out.append(_safe("secrets", "Camera secrets", lambda: check_secrets(env)))
    out.append(_safe("config", "Plugin settings", lambda: check_config(env)))
    out.append(_safe("engine", "Watcher", lambda: check_engine(env)))
    out.append(_safe("storage", "Storage", lambda: check_storage(env)))
    out.append(_safe("delivery", "Alert destination", lambda: check_delivery(env)))
    out.append(_safe("budget", "Vision call caps", lambda: check_budget(env)))
    out.append(_safe("rules", "Rules", lambda: check_rules(env)))
    return [c for c in out if c is not None]
