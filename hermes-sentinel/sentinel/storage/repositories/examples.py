"""Teaching examples attached to rules."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..constants import EXAMPLE_KINDS, MAX_IMAGE_BYTES, SAFE_ID_RE
from ..database import Database
from ..errors import StoreError
from ..files import ImageFiles
from ..records import ExampleRecord, clip, to_iso
from ..retention import RetentionPolicy
from .events import EventRepository
from .rules import RuleRepository

_COLUMNS = "id, rule_id, kind, path, event_id, note, created_at"


class ExampleRepository:
    """Example rows plus their image files, capped per rule and kind."""

    def __init__(
        self,
        database: Database,
        images: ImageFiles,
        clock: Callable[[], float],
        policy: RetentionPolicy,
        rules: RuleRepository,
        events: EventRepository,
    ) -> None:
        """Bind to the shared database, files, clock, policy and sibling repositories."""
        self._db = database
        self._images = images
        self._clock = clock
        self._policy = policy
        self._rules = rules
        self._events = events

    def add_example(
        self,
        rule_id: str,
        kind: str,
        jpeg: bytes | None = None,
        *,
        path: Path | str | None = None,
        event_id: int | None = None,
        note: str = "",
    ) -> ExampleRecord:
        """Attach a teaching example.

        Give either ``jpeg`` bytes, an existing ``path`` inside
        the data dir (copied), or an ``event_id`` whose snapshot is copied. Keeps at most
        ``max_examples_per_kind`` per rule and kind (oldest dropped with its file).
        """
        if kind not in EXAMPLE_KINDS:
            raise ValueError(f"kind must be one of {EXAMPLE_KINDS}")
        if self._rules.get_rule(rule_id) is None:
            raise StoreError(f"unknown rule {rule_id!r}")
        if jpeg is None:
            jpeg = self._read_source(path, event_id)
        rel = self._store_image(rule_id, kind, jpeg)
        example_id, stale = self._insert(rule_id, kind, rel, event_id, note)
        for _, p in stale:
            self._images.unlink(p)
        rows = self._db.read(f"SELECT {_COLUMNS} FROM examples WHERE id = ?", (example_id,))
        return self._example(rows[0])

    def _read_source(self, path: Path | str | None, event_id: int | None) -> bytes:
        """Bytes of the image named by ``path`` or by ``event_id``'s snapshot."""
        src: Path | None = None
        if path is not None:
            src = self._images.abs(self._images.rel(path))
        elif event_id is not None:
            ev = self._events.get_event(event_id)
            if ev is None:
                raise StoreError(f"unknown event {event_id}")
            src = Path(ev.snapshot_path) if ev.snapshot_path else None
        if src is None:
            raise ValueError("an example needs jpeg bytes, a path or an event with a snapshot")
        try:
            if src.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError(f"image larger than {MAX_IMAGE_BYTES} bytes")
            return src.read_bytes()
        except OSError as exc:
            raise StoreError(f"cannot read example image {src}: {exc}") from exc

    def _store_image(self, rule_id: str, kind: str, jpeg: bytes) -> str:
        """Write the image under ``examples/<rule>/``; returns its relative path."""
        safe_rule = SAFE_ID_RE.sub("", rule_id)[:64] or "rule"
        stamp = to_iso(self._clock()).replace(":", "").replace(".", "")
        stem = f"{kind}_{stamp}_" + uuid.uuid4().hex[:6]
        written = self._images.write_image(self._images.example_dir(safe_rule), stem, jpeg)
        return self._images.rel(written)

    def _insert(
        self, rule_id: str, kind: str, rel: str, event_id: int | None, note: str
    ) -> tuple[int, list[Any]]:
        """Insert the row and evict the oldest beyond the cap; returns ``(id, evicted rows)``."""
        try:
            with self._db.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO examples(rule_id, kind, path, event_id, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rule_id, kind, rel, event_id, clip(note, 500), to_iso(self._clock())),
                )
                stale = self._evict_oldest(conn, rule_id, kind)
                return int(cur.lastrowid), stale
        except BaseException:
            self._images.unlink(rel)
            raise

    def _evict_oldest(self, conn: sqlite3.Connection, rule_id: str, kind: str) -> list[Any]:
        """Delete rows past ``max_examples_per_kind``; returns them for file removal."""
        stale = conn.execute(
            "SELECT id, path FROM examples WHERE rule_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT -1 OFFSET ?",
            (rule_id, kind, self._policy.max_examples_per_kind),
        ).fetchall()
        conn.executemany("DELETE FROM examples WHERE id = ?", [(r[0],) for r in stale])
        return stale

    def _example(self, row: tuple[Any, ...]) -> ExampleRecord:
        p = self._images.abs(row[3])
        return ExampleRecord(
            id=int(row[0]), rule_id=row[1], kind=row[2], path=str(p) if p else "",
            event_id=row[4], note=row[5], created_at=row[6],
        )  # fmt: skip

    def list_examples(
        self, rule_id: str, kind: str | None = None, *, limit: int | None = None
    ) -> list[ExampleRecord]:
        """Newest first."""
        sql = f"SELECT {_COLUMNS} FROM examples WHERE rule_id = ?"
        params: list[Any] = [rule_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [self._example(r) for r in self._db.read(sql, params)]

    def delete_example(self, example_id: int) -> bool:
        """Delete one example row and its file; False if it does not exist."""
        with self._db.tx() as conn:
            row = conn.execute("SELECT path FROM examples WHERE id = ?", (example_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM examples WHERE id = ?", (example_id,))
        self._images.unlink(row[0])
        return True
