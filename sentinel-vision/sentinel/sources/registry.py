"""Factory that turns a spec into the matching frame source."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from .backends import (
    CameraSource,
    FileSource,
    FolderSource,
    ScreenSource,
    SnapshotUrlSource,
    StreamSource,
)
from .base import BaseSource, FrameSource
from .spec import SourceSpec, require_configured

SOURCE_CLASSES: dict[str, type[BaseSource]] = {
    "camera": CameraSource,
    "stream": StreamSource,
    "snapshot": SnapshotUrlSource,
    "screen": ScreenSource,
    "folder": FolderSource,
    "file": FileSource,
}


def open_source(
    spec: SourceSpec | str | int | Mapping[str, Any],
    *,
    named_streams: Mapping[str, str] | None = None,
    trusted_sources: Collection[str] = (),
    clock: Callable[[], float] | None = None,
    auto_open: bool = False,
) -> FrameSource:
    """Build the frame source for ``spec`` (see the package docstring for the syntax).

    The source is returned unopened unless ``auto_open`` is true, so callers control when
    devices are acquired. Raises :class:`SourceSpecError` for a bad spec, including a network
    URL that is not from the user's config (``named_streams`` / ``trusted_sources``).
    """
    parsed = SourceSpec.parse(spec, named_streams=named_streams, trusted_sources=trusted_sources)
    require_configured(parsed.kind, parsed.configured)
    source = SOURCE_CLASSES[parsed.kind](parsed, clock=clock)
    if auto_open:
        source.open()
    return source
