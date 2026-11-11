"""Image files under the plugin data directory (snapshots and teaching examples)."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import suppress
from pathlib import Path

from .constants import EXAMPLE_DIR, JPEG_MAGIC, MAX_IMAGE_BYTES, SNAPSHOT_DIR
from .database import Database
from .errors import StoreError

logger = logging.getLogger(__name__)

SnapshotFile = tuple[str, Path, int, float]


def _file_size(path: str) -> int:
    """Size of ``path`` in bytes, 0 when it cannot be stat-ed."""
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


class ImageFiles:
    """Path safety, atomic JPEG writes and size accounting inside the data directory."""

    def __init__(self, database: Database) -> None:
        """Bind to ``database`` for its data directory."""
        self._data_dir = database.data_dir

    def rel(self, path: Path | str) -> str:
        """Path relative to the data dir; raises if it points outside."""
        p = Path(path)
        p = (p if p.is_absolute() else self._data_dir / p).resolve()
        try:
            return p.relative_to(self._data_dir).as_posix()
        except ValueError:
            raise StoreError(f"path is outside the plugin data directory: {path}") from None

    def abs(self, rel: str | None) -> Path | None:
        """Absolute path for a stored relative path, or ``None`` if empty/outside."""
        if not rel:
            return None
        p = (self._data_dir / rel).resolve()
        try:
            p.relative_to(self._data_dir)
        except ValueError:
            return None
        return p

    def unlink(self, rel: str | None) -> int:
        """Delete a data-dir file; returns bytes freed. Never touches paths outside."""
        p = self.abs(rel)
        if p is None:
            return 0
        try:
            size = p.stat().st_size
            p.unlink()
            return size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            logger.warning("sentinel: could not delete %s: %s", p, exc)
            return 0

    def write_image(self, folder: Path, stem: str, data: bytes) -> Path:
        """Atomically write validated JPEG ``data`` to ``folder/stem.jpg``."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("image data must be bytes")
        data = bytes(data)
        if not data.startswith(JPEG_MAGIC):
            raise ValueError("image data is not a JPEG")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image larger than {MAX_IMAGE_BYTES} bytes")
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / f"{stem}.jpg"
        tmp = folder / f".{stem}.{uuid.uuid4().hex[:6]}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except OSError as exc:
            with suppress(OSError):
                tmp.unlink()
            raise StoreError(f"could not write image {final}: {exc}") from exc
        return final

    def snapshot_dir(self, day: str | None = None) -> Path:
        """The snapshots root, or its ``YYYY-MM-DD`` folder."""
        root = self._data_dir / SNAPSHOT_DIR
        return root / day if day else root

    def example_dir(self, safe_rule: str) -> Path:
        """Folder holding one rule's teaching examples."""
        return self._data_dir / EXAMPLE_DIR / safe_rule

    def storage_bytes(self) -> int:
        """Bytes used by snapshot and example images."""
        total = 0
        for sub in (SNAPSHOT_DIR, EXAMPLE_DIR):
            root = self._data_dir / sub
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                total += sum(_file_size(os.path.join(dirpath, name)) for name in files)
        return total

    def snapshot_files(self) -> list[SnapshotFile]:
        """``(day, path, size, mtime)`` for every snapshot file, oldest first."""
        out: list[SnapshotFile] = []
        root = self.snapshot_dir()
        if not root.is_dir():
            return out
        for day_dir in root.iterdir():
            if day_dir.is_dir():
                out.extend(self._day_files(day_dir))
        out.sort(key=lambda t: (t[0], t[1].name))
        return out

    @staticmethod
    def _day_files(day_dir: Path) -> list[SnapshotFile]:
        """Regular files in one day folder, skipping unreadable entries."""
        found: list[SnapshotFile] = []
        for f in day_dir.iterdir():
            try:
                st = f.stat()
            except OSError:
                continue
            if f.is_file():
                found.append((day_dir.name, f, st.st_size, st.st_mtime))
        return found
