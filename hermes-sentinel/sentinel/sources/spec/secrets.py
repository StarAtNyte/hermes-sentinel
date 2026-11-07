"""Secret resolution for ``${NAME}`` placeholders, with an in-memory fallback cache.

Secrets are read through ``agent.secret_scope.get_secret`` (never ``os.environ``). In a
single-profile Hermes that works on any thread, boot threads included. On a multiplexing
gateway the scope is a context variable that only exists inside a tool or command call, so a
capture thread started at boot gets ``UnscopedSecretError``. To keep such cameras working the
last value read inside a scope is remembered here, keyed by that scope's profile home and in
memory only: never written to disk, cleared on unload, always replaced by a fresh read when a
scope is available. A scope-less thread only gets a value when one scope alone holds the name.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from ..errors import SecretScopeUnavailable, SourceError

log = logging.getLogger(__name__)

__all__ = [
    "SECRET_CACHE",
    "SecretCache",
    "clear_cached_secrets",
    "get_secret",
    "prime_secrets",
    "require_secret",
]


class SecretCache:
    """Thread-safe (scope, name) -> value map that lives only in process memory.

    Each value is remembered under the identity of the secret scope it was read in, so two
    profiles on a multiplexing gateway never share an entry. A scope-less reader can recall a
    name only when exactly one scope holds it; with several, the answer is ``None``.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._lock = threading.Lock()  # guards _values
        self._values: dict[tuple[str | None, str], str] = {}

    def remember(self, name: str, value: str, *, scope: str | None = None) -> None:
        """Store ``value`` for ``name`` as read in ``scope``."""
        with self._lock:
            self._values[(scope, name)] = value

    def forget(self, name: str, *, scope: str | None = None) -> None:
        """Drop ``name`` for ``scope`` (the secret was unset or removed there)."""
        with self._lock:
            self._values.pop((scope, name), None)

    def recall(self, name: str, *, scope: str | None = None) -> str | None:
        """The value remembered for ``name``, or ``None`` when unknown or ambiguous.

        With a ``scope`` only that scope's entry counts. Without one, the value is returned
        only when a single scope has remembered ``name``.
        """
        with self._lock:
            if scope is not None:
                return self._values.get((scope, name))
            found = {value for (_, key), value in self._values.items() if key == name}
            owners = [owner for (owner, key) in self._values if key == name]
            return found.pop() if len(owners) == 1 else None

    def clear(self) -> None:
        """Forget everything (plugin unload)."""
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        """Number of remembered secrets."""
        with self._lock:
            return len(self._values)


SECRET_CACHE = SecretCache()


def _is_unscoped(exc: BaseException) -> bool:
    """Whether ``exc`` is Hermes' fail-closed "no secret scope on this task" error."""
    return any(cls.__name__ == "UnscopedSecretError" for cls in type(exc).__mro__)


def _hermes_get_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret as hermes_get_secret
    except ImportError as exc:
        raise SourceError(
            f"cannot resolve secret {name!r}: Hermes secret scope is unavailable"
        ) from exc
    value = hermes_get_secret(name)
    return None if value is None else str(value)


def _current_scope_identity() -> str | None:
    """Identity (profile home) of the secret scope bound to this task, if Hermes exposes one."""
    try:
        from agent.secret_scope import current_secret_scope_home
    except ImportError:
        return None
    return current_secret_scope_home()


def get_secret(name: str, *, cache: SecretCache = SECRET_CACHE) -> str | None:
    """Read one secret through Hermes' secret scope, falling back to the in-memory cache.

    Raises:
        SecretScopeUnavailable: no scope on this thread and nothing cached yet.
        SourceError: the scope module is missing or the read failed otherwise.
    """
    try:
        value = _hermes_get_secret(name)
    except SourceError:
        raise
    except Exception as exc:  # the scope may raise its own fail-closed error type
        if not _is_unscoped(exc):
            raise SourceError(f"cannot resolve secret {name!r}: {type(exc).__name__}") from exc
        cached = cache.recall(name)
        if cached is not None:
            return cached
        raise SecretScopeUnavailable(
            f"waiting for secret scope: {name!r} is only readable inside a Hermes tool or "
            "command call; use /sentinel resume or any sentinel tool once"
        ) from exc
    scope = _current_scope_identity()
    if value:
        cache.remember(name, value, scope=scope)
    else:
        cache.forget(name, scope=scope)
    return value


def require_secret(name: str, *, origin: str) -> str:
    """Return the secret ``name`` or raise :class:`SourceError` if it is unset."""
    value = get_secret(name)
    if not value:
        raise SourceError(
            f"{origin}: secret {name!r} is not set; add it to Hermes' secrets/.env for this profile"
        )
    return value


def prime_secrets(names: Iterable[str], *, cache: SecretCache = SECRET_CACHE) -> int:
    """Read ``names`` now (call from inside a scope) so capture threads can use them later.

    Returns how many were found. Never raises: an unreadable name is skipped and logged.
    """
    found = 0
    for name in names:
        try:
            if get_secret(name, cache=cache):
                found += 1
        except SourceError as exc:
            log.debug("sentinel: could not prime secret %s: %s", name, exc)
    return found


def clear_cached_secrets() -> None:
    """Forget every cached secret (plugin unload)."""
    SECRET_CACHE.clear()
