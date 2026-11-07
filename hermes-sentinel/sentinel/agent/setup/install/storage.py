"""Check that the data directory is writable and has free space."""

from __future__ import annotations

from ..env import SetupEnv
from ..masking import mask
from ..model import FAIL, OK, WARN, Check

__all__ = ["LOW_DISK_FAIL_MB", "LOW_DISK_WARN_MB", "check_storage"]

LOW_DISK_WARN_MB = 500
LOW_DISK_FAIL_MB = 50


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
    except Exception as exc:  # noqa: BLE001
        return Check(
            "storage",
            title,
            FAIL,
            f"Cannot open or write the data directory: {mask(exc)}",
            "Check permissions and free space of HERMES_HOME/plugin-data/hermes-sentinel.",
        )
    try:
        free = float(env.disk_free_mb(path))
    except Exception:  # noqa: BLE001
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
