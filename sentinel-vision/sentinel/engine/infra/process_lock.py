"""Exclusive lock file so two Hermes processes never open the same camera."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ProcessLock"]


def _try_flock(handle: Any) -> bool:
    """Take the OS lock on ``handle``; True when held (or no locking primitive exists)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return _try_msvcrt_lock(handle)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _try_msvcrt_lock(handle: Any) -> bool:  # pragma: no cover - Windows
    """Windows counterpart of :func:`_try_flock`."""
    try:
        import msvcrt
    except ImportError:
        return True  # single-process assumption
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


class ProcessLock:
    """Exclusive, non-blocking lock on a file (``fcntl``/``msvcrt``); released on close.

    Where neither is available the lock always succeeds (single-process assumption).
    """

    def __init__(self, path: Path | str) -> None:
        """Lock the file at ``path`` (created on first acquire)."""
        self.path = Path(path)
        self._handle: Any = None
        self._lock = threading.Lock()  # guards _handle (open/close of the lock file)

    @property
    def held(self) -> bool:
        """True while this object owns the lock."""
        return self._handle is not None

    def acquire(self) -> bool:
        """Try to take the lock without blocking; True when this process holds it."""
        with self._lock:
            if self._handle is not None:
                return True
            handle = self._open()
            if handle is None:
                return False
            if not _try_flock(handle):
                handle.close()
                return False
            with suppress(OSError):
                handle.seek(0)
                handle.truncate()
                handle.write(str(os.getpid()).encode())
                handle.flush()
            self._handle = handle
            return True

    def _open(self) -> Any:
        """The lock file opened for appending, or ``None`` when it cannot be opened."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return open(self.path, "a+b")  # noqa: SIM115 - kept open while held
        except OSError as exc:
            logger.warning("sentinel: cannot open lock file %s: %s", self.path, exc)
            return None

    def release(self) -> None:
        """Drop the lock (idempotent)."""
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            with suppress(OSError):
                handle.close()  # closing drops flock / msvcrt locks
