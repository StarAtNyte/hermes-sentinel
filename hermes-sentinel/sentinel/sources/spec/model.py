"""The validated :class:`SourceSpec` value object."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..constants import SourceKind
from ..timeouts import DEFAULT_TIMEOUT_S
from .urls import redact_url
from .validation import validate_spec

_OPTIONAL_FIELDS = ("width", "height", "username_secret", "password_secret", "token_secret")


@dataclass(frozen=True)
class SourceSpec:
    """Typed, validated description of a frame source.

    Attributes:
        kind: one of ``camera``, ``stream``, ``snapshot``, ``screen``, ``folder``.
        target: camera index (int), URL template (str), monitor index (int) or folder path.
        name: display name; defaults to a redacted rendering of the target.
        region: screen region ``(left, top, width, height)`` relative to the monitor.
        timeout: open/read timeout in seconds (network and device probing).
        width, height: requested camera capture resolution (best effort).
        username_secret, password_secret: secret names for HTTP basic auth (snapshot).
        token_secret: secret name for an HTTP bearer token (snapshot).
    """

    kind: SourceKind
    target: int | str
    name: str | None = None
    region: tuple[int, int, int, int] | None = None
    timeout: float = DEFAULT_TIMEOUT_S
    width: int | None = None
    height: int | None = None
    username_secret: str | None = None
    password_secret: str | None = None
    token_secret: str | None = None

    def __post_init__(self) -> None:
        """Validate and normalise every field."""
        validate_spec(self)

    @property
    def display_name(self) -> str:
        """Human-readable name that never contains a credential."""
        if self.name:
            return self.name
        return self.canonical()

    def canonical(self) -> str:
        """Spec string that round-trips through :meth:`parse` (credentials redacted)."""
        if self.kind == "camera":
            return f"camera:{self.target}"
        if self.kind == "screen":
            base = f"screen:{self.target}"
            if self.region:
                base += "@" + ",".join(str(v) for v in self.region)
            return base
        if self.kind == "folder":
            return f"folder:{self.target}"
        return f"{self.kind}:{redact_url(str(self.target))}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (credentials redacted, secret *names* kept)."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "spec": self.canonical(),
            "name": self.display_name,
        }
        if self.region:
            out["region"] = list(self.region)
        for attr in _OPTIONAL_FIELDS:
            if getattr(self, attr) is not None:
                out[attr] = getattr(self, attr)
        out["timeout"] = self.timeout
        return out

    @classmethod
    def parse(
        cls,
        value: SourceSpec | str | int | Mapping[str, Any],
        *,
        named_streams: Mapping[str, str] | None = None,
    ) -> SourceSpec:
        """Build a spec from a spec string, camera index, mapping, or existing spec.

        ``named_streams`` is the ``stream_urls`` config (name -> URL template); a bare name
        or ``stream:<name>`` resolves through it.
        """
        # Imported here because the parser builds SourceSpec instances.
        from .parser import parse_spec

        return parse_spec(value, named_streams=named_streams)
