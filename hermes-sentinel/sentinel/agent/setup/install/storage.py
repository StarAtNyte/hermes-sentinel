"""Check that the data directory is writable and has free space."""

from __future__ import annotations

import logging
from pathlib import Path

from ..env import SetupEnv
from ..masking import mask
from ..model import FAIL, OK, WARN, Check

logger = logging.getLogger(__name__)

__all__ = ["LOW_DISK_FAIL_MB", "LOW_DISK_WARN_MB", "check_storage"]

LOW_DISK_WARN_MB = 500
LOW_DISK_FAIL_MB = 50


def _free_mb(env: SetupEnv, path: Path) -> float | None:
    """Free disk space at ``path`` in MB, ``None`` when it cannot be read."""
    try:
        return float(env.disk_free_mb(path))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("sentinel: free disk space unreadable: %s", exc)
        return None


def check_storage(env: SetupEnv) -> Check:
    """Write a probe file into the data directory and compare the free space to the limits."""
    title = "Storage"
    if env.store_getter is None:
        return Check("storage", title, WARN, "Storage was not inspected.")
    try:
        store = env.store_getter()
        path = store.data_dir
        probe = path / ".setup-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001 - broad: the store getter is host-supplied; reported as FAIL
        logger.debug("sentinel: storage check failed: %s", type(exc).__name__)
        return Check(
            "storage",
            title,
            FAIL,
            f"Cannot open or write the data directory: {mask(exc)}",
            "Check permissions and free space of HERMES_HOME/plugin-data/hermes-sentinel.",
        )
    free = _free_mb(env, path)
    if free is None:
        return Check("storage", title, OK, f"Writable at {path}.")
    if free < LOW_DISK_FAIL_MB:
        return Check(
            "storage",
            title,
            FAIL,
            f"Only {free:.0f} MB free at {path}.",
            "Free disk space; snapshots cannot be saved.",
        )
    if free < LOW_DISK_WARN_MB:
        return Check(
            "storage",
            title,
            WARN,
            f"Writable, but only {free:.0f} MB free.",
            "Free disk space or lower max_storage_mb.",
        )
    return Check("storage", title, OK, f"Writable at {path}, {free / 1024:.1f} GB free.")
