"""Which ``${SECRET}`` names the configured sources need, and whether each can be read now."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..errors import SecretScopeUnavailable, SourceError
from .secrets import get_secret
from .urls import PLACEHOLDER_RE

__all__ = ["SecretStatus", "probe_secrets", "referenced_secrets"]


@dataclass(frozen=True)
class SecretStatus:
    """Readiness of the secrets sources reference. Holds names only, never values."""

    ready: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def referenced(self) -> tuple[str, ...]:
        """Every name checked, sorted."""
        return tuple(sorted({*self.ready, *self.waiting, *self.missing}))


def referenced_secrets(texts: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated ``${NAME}`` placeholder names found in ``texts``."""
    return tuple(sorted({m for text in texts for m in PLACEHOLDER_RE.findall(str(text))}))


def probe_secrets(
    names: Iterable[str], reader: Callable[[str], str | None] = get_secret
) -> SecretStatus:
    """Classify each name: readable now, waiting for a secret scope, or not set.

    ``reader`` raises :class:`SecretScopeUnavailable` when the thread has no scope and nothing
    is cached; any other :class:`SourceError` counts as missing.
    """
    ready: list[str] = []
    waiting: list[str] = []
    missing: list[str] = []
    for name in sorted(set(names)):
        try:
            value = reader(name)
        except SecretScopeUnavailable:
            waiting.append(name)
        except SourceError:
            missing.append(name)
        else:
            (ready if value else missing).append(name)
    return SecretStatus(tuple(ready), tuple(waiting), tuple(missing))
