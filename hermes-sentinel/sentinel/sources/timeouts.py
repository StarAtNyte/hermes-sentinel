"""Run blocking device or network calls with a deadline."""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 120.0


class CallTimeoutError(Exception):
    """A call guarded by :func:`call_with_timeout` did not finish in time."""


class _Call:
    """One guarded call: its outcome, and whether the caller gave up waiting."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.lock = threading.Lock()  # guards result, error and abandoned
        self.result: Any = None
        self.error: BaseException | None = None
        self.has_result = False
        self.abandoned = False

    def record_error(self, exc: BaseException) -> None:
        """Store the failure and wake the waiting caller."""
        with self.lock:
            self.error = exc
        self.done.set()

    def record_result(self, result: Any) -> bool:
        """Store the result and wake the caller. Returns whether the caller already left."""
        with self.lock:
            abandoned = self.abandoned
            self.result = result
            self.has_result = True
        self.done.set()
        return abandoned

    def abandon_if_pending(self) -> bool:
        """Mark the call abandoned unless it already finished. Returns whether it was marked."""
        with self.lock:
            if not self.has_result and self.error is None:
                self.abandoned = True
                return True
        return False


def call_with_timeout(
    fn: Callable[[], T],
    timeout: float,
    *,
    name: str = "sentinel-source-call",
    on_abandoned: Callable[[T], None] | None = None,
) -> T:
    """Run ``fn`` in a daemon thread and wait at most ``timeout`` seconds.

    Raises :class:`CallTimeoutError` if it does not finish. The thread cannot be killed; when
    it eventually returns, ``on_abandoned(result)`` runs so resources (a late ``VideoCapture``)
    are released instead of leaking. Exceptions from ``fn`` propagate to the caller.
    """
    call = _Call()
    # Keep the caller's contextvars (e.g. Hermes' secret scope) visible to ``fn``.
    ctx = contextvars.copy_context()

    def runner() -> None:
        try:
            result = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            call.record_error(exc)
            return
        if call.record_result(result) and on_abandoned is not None:
            try:
                on_abandoned(result)
            except Exception:  # noqa: BLE001 - best-effort cleanup in a detached thread
                log.debug("cleanup after abandoned %s failed", name, exc_info=True)

    threading.Thread(target=runner, name=name, daemon=True).start()
    finished = call.done.wait(max(0.0, timeout))
    if not finished and call.abandon_if_pending():
        raise CallTimeoutError(f"{name} did not finish within {timeout:.1f}s")
    if call.error is not None:
        raise call.error
    return call.result


def clamp_timeout(value: Any, default: float = DEFAULT_TIMEOUT_S) -> float:
    """Coerce ``value`` to a positive timeout in seconds, capped at ``MAX_TIMEOUT_S``."""
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout != timeout or timeout <= 0:  # NaN or non-positive
        return default
    return min(timeout, MAX_TIMEOUT_S)
