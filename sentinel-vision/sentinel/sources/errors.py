"""Exceptions raised by frame sources and the optional-dependency hints they carry."""

from __future__ import annotations

INSTALL_HINTS: dict[str, str] = {
    "cv2": "opencv-python-headless",
    "mss": "mss",
    "numpy": "numpy",
}


class SourceError(RuntimeError):
    """A frame source cannot produce a frame (closed, disconnected, exhausted)."""


class SourceDisconnected(SourceError):
    """The underlying device or stream went away mid-read."""


class SourceExhausted(SourceError):
    """A finite source (file, fixture replay) has no more frames."""


class SourceNotReady(SourceError):
    """The source is reachable but has nothing to deliver yet (e.g. an empty folder)."""


class SecretScopeUnavailable(SourceNotReady):
    """A ``${SECRET}`` cannot be read yet: this thread has no Hermes secret scope.

    Raised on boot and scheduler threads of a multiplexing gateway. It clears by itself once a
    tool or command call (which runs inside the scope) primes the in-memory secret cache.
    """


class SourceSpecError(SourceError, ValueError):
    """A source spec is malformed or unsafe (e.g. a literal password in a URL)."""


class MissingDependency(SourceError, ImportError):
    """An optional runtime package (OpenCV, mss, numpy) is not installed."""

    def __init__(self, module: str, purpose: str) -> None:
        """Build the message from the missing ``module`` and what ``purpose`` needed it."""
        package = INSTALL_HINTS.get(module, module)
        self.module = module
        self.package = package
        super().__init__(
            f"{purpose} needs the Python package '{package}', which is not installed. It is "
            f"declared in sentinel-vision's plugin.yaml python_dependencies: reinstall or "
            f"repair the plugin so Hermes installs its dependencies, then restart Hermes."
        )
