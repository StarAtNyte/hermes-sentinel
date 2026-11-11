"""Lazy imports of the optional runtime packages (OpenCV, mss, numpy).

Callers use the module attribute (``dependencies.require_cv2(...)``) so a single patch point
serves tests that fake a missing or stubbed package.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from .errors import INSTALL_HINTS, MissingDependency


def import_optional(module: str, purpose: str) -> ModuleType:
    """Import ``module`` or raise :class:`MissingDependency` naming the pip package."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MissingDependency(module, purpose) from exc


def require_cv2(purpose: str) -> ModuleType:
    """Return the ``cv2`` module or raise :class:`MissingDependency`."""
    return import_optional("cv2", purpose)


def require_numpy(purpose: str) -> ModuleType:
    """Return the ``numpy`` module or raise :class:`MissingDependency`."""
    return import_optional("numpy", purpose)


def dependency_status() -> dict[str, str | None]:
    """Map each optional runtime module to ``None`` (importable) or an install hint."""
    status: dict[str, str | None] = {}
    for module, package in INSTALL_HINTS.items():
        try:
            importlib.import_module(module)
            status[module] = None
        except ImportError:
            status[module] = f"missing Python package '{package}'"
    return status
