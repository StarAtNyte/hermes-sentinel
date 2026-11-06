"""Snapshots and events."""

from __future__ import annotations

import logging
import math
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..constants import (
    FEEDBACK_VALUES,
    MAX_CAPTION_LEN,
    MAX_QUERY_LIMIT,
    MAX_REASON_LEN,
    SAFE_ID_RE,
)
from ..database import Database
from ..files import ImageFiles
from ..records import EventRecord, as_iso, clip, to_iso
from . import event_search
from .event_search import EVENT_COLS

logger = logging.getLogger(__name__)


def _clamp_confidence(confidence: float | None) -> float | None:
    """Clamp to ``[0, 1]``; non-finite values become ``None``."""
    if confidence is None:
        return None
    conf = float(confidence)
    return min(1.0, max(0.0, conf)) if math.isfinite(conf) else None


class EventRepository:
    """Event rows, their FTS index entries and snapshot files."""

    def __init__(self, database: Database, images: ImageFiles, clock: Callable[[], float]) -> None:
        """Bind to the shared database, image files and clock."""
        self._db = database
        self._images = images
        self._clock = clock

    def save_snapshot(
        self, jpeg: bytes, *, ts: datetime | float | None = None, rule_id: str = ""
    ) -> Path:
        """Write a JPEG under ``snapshots/YYYY-MM-DD/`` atomically; returns its path."""
        iso = to_iso(self._clock() if ts is None else ts)
        safe_rule = SAFE_ID_RE.sub("", rule_id)[:32] or "frame"
        stem = f"{iso[11:23].replace(':', '').replace('.', '')}_{safe_rule}_{uuid.uuid4().hex[:6]}"
        return self._images.write_image(self._images.snapshot_dir(iso[:10]), stem, jpeg)

    def add_event(
        self,
        rule_id: str,
        *,
        caption: str = "",
        reason: str = "",
        confidence: float | None = None,
        source: str = "",
        snapshot_path: Path | str | None = None,
        ts: datetime | float | str | None = None,
    ) -> EventRecord:
        """Record a confirmed event. ``snapshot_path`` must be inside the data directory."""
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("rule_id must be a non-empty string")
        conf = _clamp_confidence(confidence)
        iso = as_iso(ts) or to_iso(self._clock())
        rel = self._images.rel(snapshot_path) if snapshot_path else None
        caption_s, reason_s = clip(caption, MAX_CAPTION_LEN), clip(reason, MAX_REASON_LEN)
        with self._db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO events(rule_id, ts, caption, reason, confidence, source, "
                "snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, iso, caption_s, reason_s, conf, clip(source, 256), rel),
            )
            event_id = int(cur.lastrowid)
            if self._db.fts_enabled:
                conn.execute(
                    "INSERT INTO events_fts(rowid, caption, reason) VALUES (?, ?, ?)",
                    (event_id, caption_s, reason_s),
                )
        record = self.get_event(event_id)
        assert record is not None
        return record

    def _event(self, row: tuple[Any, ...]) -> EventRecord:
        snap = self._images.abs(row[7])
        return EventRecord(
            id=int(row[0]), rule_id=row[1], ts=row[2], caption=row[3], reason=row[4],
            confidence=row[5], source=row[6], snapshot_path=str(snap) if snap else None,
            feedback=row[8],
        )  # fmt: skip

    def get_event(self, event_id: int) -> EventRecord | None:
        """The event with ``event_id``, or ``None``."""
        rows = self._db.read(f"SELECT {EVENT_COLS} FROM events e WHERE e.id = ?", (event_id,))
        return self._event(rows[0]) if rows else None

    def last_event(self, rule_id: str) -> EventRecord | None:
        """The newest event for ``rule_id``, or ``None``."""
        rows = self._db.read(
            f"SELECT {EVENT_COLS} FROM events e WHERE e.rule_id = ? "
            "ORDER BY e.ts DESC, e.id DESC LIMIT 1",
            (rule_id,),
        )
        return self._event(rows[0]) if rows else None

    def set_feedback(self, event_id: int, feedback: str | None) -> EventRecord | None:
        """Mark an event ``correct`` / ``false_alarm`` (or clear with ``None``)."""
        if feedback is not None and feedback not in FEEDBACK_VALUES:
            raise ValueError(f"feedback must be one of {FEEDBACK_VALUES} or None")
        with self._db.tx() as conn:
            n = conn.execute(
                "UPDATE events SET feedback = ? WHERE id = ?", (feedback, event_id)
            ).rowcount
        return self.get_event(event_id) if n else None

    def count_events(self, *, rule_id: str | None = None, since: Any = None) -> int:
        """Number of events, optionally for one rule and/or since a time."""
        sql, params = "SELECT COUNT(*) FROM events WHERE 1=1", []
        if rule_id is not None:
            sql += " AND rule_id = ?"
            params.append(rule_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(as_iso(since))
        return int(self._db.read(sql, params)[0][0])

    def query_events(
        self,
        *,
        text: str | None = None,
        rule_id: str | None = None,
        since: datetime | float | str | None = None,
        until: datetime | float | str | None = None,
        feedback: str | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        """Filter events by text, rule, time range and feedback.

        ``text`` matches any meaningful word of the caption/reason (FTS5
        prefix match ranked by relevance, else ``LIKE`` newest first). Without ``text``
        results are newest first. ``since`` is inclusive, ``until`` exclusive.
        """
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        where, params = event_search.filter_clauses(
            rule_id=rule_id, since=since, until=until, feedback=feedback
        )
        terms = event_search.search_terms(text) if text else []
        if text and not terms:
            return []
        if terms and self._db.fts_enabled:
            sql, args = event_search.fts_query(terms, where, params, limit)
            try:
                return [self._event(r) for r in self._db.read(sql, args)]
            except sqlite3.OperationalError as exc:
                logger.warning("sentinel: FTS query failed (%s); using LIKE", exc)
        sql, args = event_search.like_query(terms, where, params, limit)
        return [self._event(r) for r in self._db.read(sql, args)]
