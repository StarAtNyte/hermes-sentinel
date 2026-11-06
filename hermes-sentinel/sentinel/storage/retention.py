"""Retention settings and the pruning service."""

from __future__ import annotations

import math
from collections.abc import Callable

from .constants import (
    DEFAULT_MAX_EXAMPLES_PER_KIND,
    PRUNE_INTERVAL_SECONDS,
    USAGE_KEEP_DAYS,
)
from .database import Database
from .files import ImageFiles, SnapshotFile
from .records import PruneReport, hour_bucket, to_iso


def _positive(value: float, name: str) -> float:
    """``value`` as a positive finite float, else ``ValueError``."""
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


class RetentionPolicy:
    """Validated retention limits, mutable at runtime (config reload)."""

    def __init__(
        self,
        retention_days: float = 7.0,
        max_storage_mb: float = 500.0,
        max_examples_per_kind: int = DEFAULT_MAX_EXAMPLES_PER_KIND,
    ) -> None:
        """Validate and store the limits (``ValueError`` on bad numbers)."""
        self.retention_days = retention_days
        self.max_storage_mb = max_storage_mb
        self.max_examples_per_kind = max_examples_per_kind

    @property
    def retention_days(self) -> float:
        """Days events are kept."""
        return self._retention_days

    @retention_days.setter
    def retention_days(self, value: float) -> None:
        """Set retention days."""
        self._retention_days = _positive(value, "retention_days")

    @property
    def max_storage_mb(self) -> float:
        """Image storage cap in MB."""
        return self._max_storage_mb

    @max_storage_mb.setter
    def max_storage_mb(self, value: float) -> None:
        """Set max storage mb."""
        self._max_storage_mb = _positive(value, "max_storage_mb")

    @property
    def max_examples_per_kind(self) -> int:
        """Examples kept per rule and kind."""
        return self._max_examples_per_kind

    @max_examples_per_kind.setter
    def max_examples_per_kind(self, value: int) -> None:
        """Set max examples per kind."""
        self._max_examples_per_kind = max(1, int(value))


class RetentionService:
    """Applies retention and the storage cap; remembers when it last ran."""

    def __init__(
        self,
        database: Database,
        images: ImageFiles,
        policy: RetentionPolicy,
        clock: Callable[[], float],
    ) -> None:
        """Bind to the shared database, files, policy and clock."""
        self._db = database
        self._images = images
        self._policy = policy
        self._clock = clock
        self._last_prune: float | None = None

    def prune(self, now: float | None = None) -> PruneReport:
        """Apply retention and the storage cap.

        1. Delete events older than ``retention_days`` (and their snapshots).
        2. Delete orphan snapshot files (no event row) older than one hour and stray temp
           files - leftovers from crashes between write and insert.
        3. While images exceed ``max_storage_mb``, delete the oldest snapshots; their
           events stay (history) with ``snapshot_path`` cleared. Examples are never pruned
           here (they are capped per rule instead).
        4. Drop usage buckets older than ``USAGE_KEEP_DAYS`` and empty day folders.
        """
        now = self._clock() if now is None else now
        report = PruneReport()
        self._prune_events(now, report)
        kept = self._delete_orphans(now, report)
        self._enforce_cap(kept, report)
        self._remove_empty_day_dirs()
        report.bytes_after = self._images.storage_bytes()
        with self._db.lock:
            self._last_prune = now
        return report

    def _prune_events(self, now: float, report: PruneReport) -> None:
        """Delete expired events, FTS rows and old usage buckets, then their snapshots."""
        cutoff = to_iso(now - self._policy.retention_days * 86400)
        with self._db.tx() as conn:
            old = conn.execute(
                "SELECT id, snapshot_path FROM events WHERE ts < ?", (cutoff,)
            ).fetchall()
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            if self._db.fts_enabled and old:
                conn.executemany("DELETE FROM events_fts WHERE rowid = ?", [(r[0],) for r in old])
            usage_cut = hour_bucket(now - USAGE_KEEP_DAYS * 86400)
            report.usage_rows_deleted = conn.execute(
                "DELETE FROM usage WHERE bucket < ?", (usage_cut,)
            ).rowcount
        report.events_deleted = len(old)
        for _, snap in old:
            freed = self._images.unlink(snap)
            if freed:
                report.snapshots_deleted += 1
                report.bytes_freed += freed

    def _delete_orphans(self, now: float, report: PruneReport) -> list[SnapshotFile]:
        """Remove unreferenced old snapshots and temp files; returns the files kept."""
        referenced = {
            r[0]
            for r in self._db.read(
                "SELECT snapshot_path FROM events WHERE snapshot_path IS NOT NULL"
            )
        }
        kept: list[SnapshotFile] = []
        for entry in self._images.snapshot_files():
            _day, f, _size, mtime = entry
            rel = self._images.rel(f)
            is_tmp = f.name.startswith(".") and f.name.endswith(".tmp")
            if rel not in referenced and (is_tmp or now - mtime > 3600):
                report.orphans_deleted += 1
                report.bytes_freed += self._images.unlink(rel)
            else:
                kept.append(entry)
        return kept

    def _enforce_cap(self, kept: list[SnapshotFile], report: PruneReport) -> None:
        """Delete the oldest snapshots until under the cap; clear their event paths."""
        cap = int(self._policy.max_storage_mb * 1024 * 1024)
        total = self._images.storage_bytes()
        victims: list[str] = []
        for _day, f, size, _mtime in kept:
            if total <= cap:
                break
            rel = self._images.rel(f)
            freed = self._images.unlink(rel)
            if freed or not f.exists():
                total -= size
                report.bytes_freed += freed
                report.snapshots_deleted += 1
                victims.append(rel)
        if victims:
            with self._db.tx() as conn:
                conn.executemany(
                    "UPDATE events SET snapshot_path = NULL WHERE snapshot_path = ?",
                    [(v,) for v in victims],
                )

    def _remove_empty_day_dirs(self) -> None:
        """Remove empty ``snapshots/<day>`` folders."""
        root = self._images.snapshot_dir()
        if not root.is_dir():
            return
        for day_dir in root.iterdir():
            try:
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    day_dir.rmdir()
            except OSError:
                continue

    def prune_due(self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS) -> bool:
        """True if :meth:`prune_if_due` would run a prune now."""
        now = self._clock() if now is None else now
        with self._db.lock:
            return self._last_prune is None or now - self._last_prune >= interval

    def prune_if_due(
        self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS
    ) -> PruneReport | None:
        """Run :meth:`prune` if it has not run for ``interval`` seconds (hourly by default)."""
        now = self._clock() if now is None else now
        with self._db.lock:
            if self._last_prune is not None and now - self._last_prune < interval:
                return None
        return self.prune(now)
