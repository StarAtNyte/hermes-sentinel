"""The ``FrameSource`` protocol and the shared base class of every real source."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .errors import SourceError

if TYPE_CHECKING:  # numpy is only needed for type hints here.
    import numpy as np

    from .spec import SourceSpec

    Frame = np.ndarray
else:
    Frame = object


@runtime_checkable
class FrameSource(Protocol):
    """Minimal interface shared by real and fake frame sources."""

    def open(self) -> None:
        """Acquire the device or stream. Must be called before ``read``."""
        ...

    def read(self) -> tuple[float, Frame]:
        """Return ``(timestamp_seconds, frame)`` or raise ``SourceError``."""
        ...

    def close(self) -> None:
        """Release resources. Safe to call more than once."""
        ...


class BaseSource:
    """Shared lifecycle, locking and context-manager support."""

    kind: str = "source"

    def __init__(self, spec: SourceSpec, *, clock: Callable[[], float] | None = None) -> None:
        """Bind the spec and the wall clock used to timestamp frames."""
        self.spec = spec
        self.clock: Callable[[], float] = clock or time.time
        self._lock = threading.RLock()  # serialises open/read/close on this source
        self._is_open = False

    @property
    def name(self) -> str:
        """Display name, free of credentials."""
        return self.spec.display_name

    @property
    def is_open(self) -> bool:
        """Whether the source is currently open."""
        return self._is_open

    def __repr__(self) -> str:
        """Show kind and state, never the resolved URL."""
        state = "open" if self._is_open else "closed"
        return f"<{type(self).__name__} {self.spec.canonical()} {state}>"

    def __enter__(self) -> BaseSource:
        """Open the source."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the source."""
        self.close()

    def _require_open(self) -> None:
        if not self._is_open:
            raise SourceError(f"{self.name}: read() on a source that is not open")

    def open(self) -> None:  # pragma: no cover - overridden
        """Acquire the device or stream."""
        raise NotImplementedError

    def read(self) -> tuple[float, Frame]:  # pragma: no cover - overridden
        """Return ``(timestamp_seconds, frame)``."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - overridden
        """Release resources."""
        raise NotImplementedError
