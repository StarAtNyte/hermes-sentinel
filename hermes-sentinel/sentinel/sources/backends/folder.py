"""Folder source: the newest image file in a directory."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import dependencies, frames
from ..base import BaseSource, Frame
from ..constants import IMAGE_EXTENSIONS, MAX_FOLDER_ENTRIES, MAX_IMAGE_FILE_BYTES
from ..errors import (
    MissingDependency,
    SourceDisconnected,
    SourceError,
    SourceNotReady,
    SourceSpecError,
)
from ..spec import SourceSpec

log = logging.getLogger(__name__)

Candidate = tuple[int, str, int]  # (mtime_ns, file name, size)


@dataclass
class FolderCache:
    """The last decoded frame and the (name, mtime, size) key it was decoded from."""

    key: tuple[str, int, int] | None = None
    frame: Any = None


class FolderSource(BaseSource):
    """Newest image file (by modification time) in a directory.

    Each ``read`` rescans the directory (non-recursive, at most ``MAX_FOLDER_ENTRIES``
    entries) and returns the newest image. The last decoded frame is cached by
    (path, mtime, size), so an unchanged folder costs one directory scan. A newest file that
    cannot be decoded yet (still being written) falls back to the previous frame.
    """

    kind = "folder"

    def __init__(
        self,
        spec: SourceSpec,
        *,
        clock: Callable[[], float] | None = None,
        extensions: frozenset[str] = IMAGE_EXTENSIONS,
        max_file_bytes: int = MAX_IMAGE_FILE_BYTES,
    ) -> None:
        """Require a folder spec; ``extensions`` and ``max_file_bytes`` are injectable."""
        if spec.kind != "folder":
            raise SourceSpecError(f"FolderSource needs a folder spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        self.path = Path(os.path.expanduser(str(spec.target)))
        self._extensions = frozenset(e.lower() for e in extensions)
        self._max_file_bytes = int(max_file_bytes)
        self._cache = FolderCache()

    def open(self) -> None:
        """Check the directory exists."""
        with self._lock:
            if self._is_open:
                return
            dependencies.require_cv2(f"folder source {self.name!r}")
            if not self.path.is_dir():
                raise SourceDisconnected(f"{self.name}: {self.path} is not a directory")
            self._cache = FolderCache()
            self._is_open = True

    def _candidate(self, entry: os.DirEntry[str]) -> Candidate | None:
        """Describe ``entry`` if it is a non-empty regular image file, else ``None``."""
        name = entry.name
        if name.startswith(".") or Path(name).suffix.lower() not in self._extensions:
            return None
        try:
            st = entry.stat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            return None
        return st.st_mtime_ns, name, st.st_size

    def _candidates(self) -> Iterator[Candidate]:
        with os.scandir(self.path) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_FOLDER_ENTRIES:
                    log.warning(
                        "%s: more than %d entries; scanning only the first",
                        self.name,
                        MAX_FOLDER_ENTRIES,
                    )
                    return
                candidate = self._candidate(entry)
                if candidate is not None:
                    yield candidate

    def newest(self) -> tuple[Path, int, int] | None:
        """Return ``(path, mtime_ns, size)`` of the newest image file, or ``None``."""
        best: Candidate | None = None
        try:
            for candidate in self._candidates():
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
        except FileNotFoundError:
            raise SourceDisconnected(f"{self.name}: {self.path} no longer exists") from None
        except OSError as exc:
            raise SourceDisconnected(f"{self.name}: cannot list {self.path} ({exc})") from None
        if best is None:
            return None
        return self.path / best[1], best[0], best[2]

    def read(self) -> tuple[float, Frame]:
        """Return the newest image, decoding only when the folder changed."""
        with self._lock:
            self._require_open()
            found = self.newest()
            if found is None:
                raise SourceNotReady(f"{self.name}: no images in {self.path} yet")
            path, mtime_ns, size = found
            key = (path.name, mtime_ns, size)
            if key != self._cache.key:
                self._refresh(path, key, size)
            ts = self.clock()
            return ts, self._cache.frame.copy()

    def _refresh(self, path: Path, key: tuple[str, int, int], size: int) -> None:
        """Decode ``path`` into the cache, keeping the previous frame if it is unreadable."""
        frame = self._load(path, size)
        if frame is not None:
            self._cache = FolderCache(key=key, frame=frame)
        elif self._cache.frame is None:
            raise SourceNotReady(f"{self.name}: {path.name} is not readable yet")

    def _load(self, path: Path, size: int) -> Frame | None:
        if size > self._max_file_bytes:
            log.warning("%s: skipping %s (%d bytes > cap)", self.name, path.name, size)
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            return frames.decode_image(data, origin=f"{self.name}/{path.name}")
        except MissingDependency:
            raise
        except SourceError:
            return None

    def close(self) -> None:
        """Drop the cached frame."""
        with self._lock:
            self._is_open = False
            self._cache = FolderCache()
