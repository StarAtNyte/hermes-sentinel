"""Persistent state: rules, events, teaching examples, usage counters and snapshot images.

Everything lives under the plugin's Hermes data directory:

* ``plugin_db("hermes-sentinel", "sentinel.db")`` - SQLite (WAL) with the tables below.
* ``plugin_data_dir("hermes-sentinel")/snapshots/YYYY-MM-DD/*.jpg`` - event snapshots.
* ``plugin_data_dir("hermes-sentinel")/examples/<rule_id>/*.jpg`` - teaching examples.

Schema (version tracked in ``schema_migrations``; migrations run forward on open)::

    rules     (id PK, spec_json, enabled, created_at, updated_at)
    events    (id PK, rule_id, ts, caption, reason, confidence, source, snapshot_path, feedback)
    examples  (id PK, rule_id -> rules ON DELETE CASCADE, kind pos|neg, path, event_id, ...)
    usage     (bucket 'YYYY-MM-DDTHH' UTC, rule_id, vision_calls)  PK(bucket, rule_id)
    events_fts  FTS5(caption, reason), rowid = events.id   (only when FTS5 is available)

``ts`` columns are ISO-8601 UTC with a fixed width (``2026-11-06T09:12:03.120Z``) so string
order is time order. Paths are stored relative to the data directory and returned absolute.
Free-text search uses FTS5 when the SQLite build has it and falls back to ``LIKE`` otherwise.

Thread safety: one connection (``check_same_thread=False``) guarded by a re-entrant lock;
writes use ``BEGIN IMMEDIATE`` so another process (the dashboard) can share the file.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .rules import RuleSpec, RuleValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "PLUGIN_NAME",
    "DB_FILENAME",
    "SCHEMA_VERSION",
    "EventRecord",
    "ExampleRecord",
    "PruneReport",
    "Store",
    "StoreError",
    "UsageResult",
    "day_range",
    "hour_bucket",
    "parse_ts",
    "to_iso",
]

PLUGIN_NAME = "hermes-sentinel"
DB_FILENAME = "sentinel.db"
SNAPSHOT_DIR = "snapshots"
EXAMPLE_DIR = "examples"

FEEDBACK_VALUES = ("correct", "false_alarm")
EXAMPLE_KINDS = ("pos", "neg")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_CAPTION_LEN = 2000
MAX_REASON_LEN = 2000
MAX_QUERY_LIMIT = 500
DEFAULT_MAX_EXAMPLES_PER_KIND = 10
USAGE_KEEP_DAYS = 35
PRUNE_INTERVAL_SECONDS = 3600.0
_JPEG_MAGIC = b"\xff\xd8\xff"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_TERM_RE = re.compile(r"[\w]+", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "anything", "are", "at", "be", "did", "do", "does", "for",
        "from", "happened", "has", "have", "i", "in", "is", "it", "me", "my", "of", "on",
        "or", "the", "to", "today", "was", "were", "what", "when", "where", "which", "who",
        "with", "yesterday",
    }
)


class StoreError(RuntimeError):
    """The store could not be opened or an operation failed irrecoverably."""


# --------------------------------------------------------------------------- time helpers


def to_iso(when: datetime | float | int | None = None) -> str:
    """Fixed-width ISO-8601 UTC (millisecond precision, ``Z`` suffix)."""
    if when is None:
        dt = datetime.now(UTC)
    elif isinstance(when, datetime):
        dt = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    elif isinstance(when, (int, float)) and not isinstance(when, bool):
        if not math.isfinite(float(when)):
            raise ValueError("timestamp must be finite")
        dt = datetime.fromtimestamp(float(when), UTC)
    else:
        raise TypeError(f"unsupported timestamp {when!r}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 string (``Z`` or offset; naive = UTC) into an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _as_iso(when: datetime | float | int | str | None) -> str | None:
    if when is None:
        return None
    if isinstance(when, str):
        return to_iso(parse_ts(when))
    return to_iso(when)


def hour_bucket(when: datetime | float | int) -> str:
    """``YYYY-MM-DDTHH`` (UTC) bucket for usage counters."""
    return to_iso(when)[:13]


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class EventRecord:
    """One confirmed event. ``snapshot_path`` is absolute (or ``None`` once pruned)."""

    id: int
    rule_id: str
    ts: str
    caption: str
    reason: str
    confidence: float | None
    source: str
    snapshot_path: str | None
    feedback: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExampleRecord:
    """A teaching example image attached to a rule."""

    id: int
    rule_id: str
    kind: str
    path: str
    event_id: int | None
    note: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UsageResult:
    """Outcome of :meth:`Store.try_consume_usage`."""

    allowed: bool
    hour_used: int
    day_used: int


@dataclass
class PruneReport:
    """What :meth:`Store.prune` removed."""

    events_deleted: int = 0
    snapshots_deleted: int = 0
    bytes_freed: int = 0
    usage_rows_deleted: int = 0
    orphans_deleted: int = 0
    bytes_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- migrations


def _run_statements(conn: sqlite3.Connection, script: str) -> None:
    """Run ``;``-separated DDL one statement at a time. (``executescript`` would COMMIT the
    surrounding migration transaction.) Statements here never contain a literal ``;``."""
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migration_1(conn: sqlite3.Connection) -> None:
    _run_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS rules (
            id          TEXT PRIMARY KEY,
            spec_json   TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id       TEXT NOT NULL,
            ts            TEXT NOT NULL,
            caption       TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL DEFAULT '',
            confidence    REAL,
            source        TEXT NOT NULL DEFAULT '',
            snapshot_path TEXT,
            feedback      TEXT CHECK (feedback IS NULL OR feedback IN ('correct','false_alarm'))
        );
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS events_rule_ts ON events(rule_id, ts);
        CREATE TABLE IF NOT EXISTS examples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id     TEXT NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL CHECK (kind IN ('pos','neg')),
            path        TEXT NOT NULL,
            event_id    INTEGER,
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS examples_rule ON examples(rule_id, kind, id);
        CREATE TABLE IF NOT EXISTS usage (
            bucket        TEXT NOT NULL,
            rule_id       TEXT NOT NULL DEFAULT '',
            vision_calls  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bucket, rule_id)
        );
        """
    )


#: Ordered (version, migration). Append new versions; never edit a shipped one.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_1),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


# --------------------------------------------------------------------------- store


def _default_connect(name: str, filename: str) -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db

    return plugin_db(name, filename)


def _default_data_dir(name: str) -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir(name))


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


class Store:
    """SQLite-backed persistence for Sentinel. Create once per plugin load; ``close()`` on
    unload. All public methods are thread-safe."""

    def __init__(
        self,
        *,
        plugin_name: str = PLUGIN_NAME,
        db_filename: str = DB_FILENAME,
        connect: Callable[[str, str], sqlite3.Connection] | None = None,
        data_dir: Path | str | None = None,
        clock: Callable[[], float] = time.time,
        retention_days: float = 7.0,
        max_storage_mb: float = 500.0,
        max_examples_per_kind: int = DEFAULT_MAX_EXAMPLES_PER_KIND,
        enable_fts: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._closed = False
        try:
            self.retention_days = retention_days
            self.max_storage_mb = max_storage_mb
        except (TypeError, ValueError) as exc:
            raise StoreError(f"invalid sentinel store settings: {exc}") from exc
        self.max_examples_per_kind = max(1, int(max_examples_per_kind))
        self._last_prune: float | None = None
        try:
            self._data_dir = (
                Path(data_dir) if data_dir is not None else _default_data_dir(plugin_name)
            ).resolve()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._conn = (connect or _default_connect)(plugin_name, db_filename)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise StoreError(f"cannot open sentinel store: {exc}") from exc
        self._conn.isolation_level = None  # explicit transactions only
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            self._fts = self._setup_fts() if enable_fts else False
        except BaseException:
            self._conn.close()
            raise

    # ---- lifecycle ------------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def fts_enabled(self) -> bool:
        return self._fts

    @property
    def retention_days(self) -> float:
        return self._retention_days

    @retention_days.setter
    def retention_days(self, value: float) -> None:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("retention_days must be a positive number")
        self._retention_days = value

    @property
    def max_storage_mb(self) -> float:
        return self._max_storage_mb

    @max_storage_mb.setter
    def max_storage_mb(self, value: float) -> None:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("max_storage_mb must be a positive number")
        self._max_storage_mb = value

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._conn.close()
                except sqlite3.Error:  # pragma: no cover - close rarely fails
                    logger.debug("sentinel store close failed", exc_info=True)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def schema_version(self) -> int:
        with self._lock:
            self._check_open()
            row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)

    # ---- internals ------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (rollback on error) under the lock."""
        with self._lock:
            self._check_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            self._check_open()
            return self._conn.execute(sql, params).fetchall()

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        current = int(
            self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        )
        if current > SCHEMA_VERSION:
            raise StoreError(
                f"sentinel database schema v{current} is newer than this plugin supports "
                f"(v{SCHEMA_VERSION}); update hermes-sentinel"
            )
        for version, migrate in MIGRATIONS:
            if version <= current:
                continue
            with self._tx() as conn:
                migrate(conn)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, to_iso(self._clock())),
                )
            logger.info("sentinel store migrated to schema v%d", version)

    def _setup_fts(self) -> bool:
        """Create/repair the FTS5 index. Returns False (LIKE fallback) when unavailable."""
        try:
            with self._tx() as conn:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5("
                    "caption, reason, tokenize='unicode61')"
                )
                n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                n_fts = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
                if n_events != n_fts:  # written while FTS was unavailable: rebuild
                    conn.execute("DELETE FROM events_fts")
                    conn.execute(
                        "INSERT INTO events_fts(rowid, caption, reason) "
                        "SELECT id, caption, reason FROM events"
                    )
            return True
        except sqlite3.OperationalError as exc:
            logger.info("sentinel: FTS5 unavailable (%s); caption search uses LIKE", exc)
            return False

    def _rel(self, path: Path | str) -> str:
        """Path relative to the data dir; raises if it points outside."""
        p = Path(path)
        p = (p if p.is_absolute() else self._data_dir / p).resolve()
        try:
            return p.relative_to(self._data_dir).as_posix()
        except ValueError:
            raise StoreError(f"path is outside the plugin data directory: {path}") from None

    def _abs(self, rel: str | None) -> Path | None:
        if not rel:
            return None
        p = (self._data_dir / rel).resolve()
        try:
            p.relative_to(self._data_dir)
        except ValueError:
            return None
        return p

    def _unlink(self, rel: str | None) -> int:
        """Delete a data-dir file; returns bytes freed. Never touches paths outside."""
        p = self._abs(rel)
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

    def _write_image(self, folder: Path, stem: str, data: bytes) -> Path:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("image data must be bytes")
        data = bytes(data)
        if not data.startswith(_JPEG_MAGIC):
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

    # ---- rules ----------------------------------------------------------------------

    def upsert_rule(self, spec: RuleSpec) -> RuleSpec:
        """Insert or replace a rule (validated first)."""
        spec.validate()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO rules(id, spec_json, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "spec_json=excluded.spec_json, enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (spec.id, spec.to_json(), int(spec.enabled), spec.created_at, spec.updated_at),
            )
        return spec

    def _row_to_spec(self, rule_id: str, spec_json: str) -> RuleSpec | None:
        try:
            return RuleSpec.from_json(spec_json)
        except RuleValidationError as exc:
            logger.warning("sentinel: skipping unreadable rule %s: %s", rule_id, exc)
            return None

    def get_rule(self, rule_id: str) -> RuleSpec | None:
        rows = self._read("SELECT id, spec_json FROM rules WHERE id = ?", (rule_id,))
        return self._row_to_spec(*rows[0]) if rows else None

    def list_rules(self, *, enabled_only: bool = False) -> list[RuleSpec]:
        sql = "SELECT id, spec_json FROM rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at, id"
        return [s for s in (self._row_to_spec(*r) for r in self._read(sql)) if s is not None]

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> RuleSpec | None:
        """Pause/resume. Returns the updated spec, or ``None`` if the rule does not exist."""
        with self._lock:
            spec = self.get_rule(rule_id)
            if spec is None:
                return None
            return self.upsert_rule(spec.with_changes(enabled=bool(enabled)))

    def delete_rule(self, rule_id: str, *, purge_events: bool = False) -> bool:
        """Delete a rule and its examples (rows and files). Events are kept as history
        unless ``purge_events`` (then their snapshots go too)."""
        with self._tx() as conn:
            ex_paths = [r[0] for r in conn.execute(
                "SELECT path FROM examples WHERE rule_id = ?", (rule_id,))]
            ev_rows: list[tuple[int, str | None]] = []
            if purge_events:
                ev_rows = conn.execute(
                    "SELECT id, snapshot_path FROM events WHERE rule_id = ?", (rule_id,)
                ).fetchall()
                conn.execute("DELETE FROM events WHERE rule_id = ?", (rule_id,))
                if self._fts:
                    conn.executemany(
                        "DELETE FROM events_fts WHERE rowid = ?", [(r[0],) for r in ev_rows]
                    )
            deleted = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount
        for p in ex_paths:
            self._unlink(p)
        for _, snap in ev_rows:
            self._unlink(snap)
        return bool(deleted)

    # ---- snapshots & events ---------------------------------------------------------

    def save_snapshot(
        self, jpeg: bytes, *, ts: datetime | float | None = None, rule_id: str = ""
    ) -> Path:
        """Write a JPEG under ``snapshots/YYYY-MM-DD/`` atomically; returns its path."""
        iso = to_iso(self._clock() if ts is None else ts)
        day = iso[:10]
        safe_rule = _SAFE_ID_RE.sub("", rule_id)[:32] or "frame"
        stem = f"{iso[11:23].replace(':', '').replace('.', '')}_{safe_rule}_{uuid.uuid4().hex[:6]}"
        return self._write_image(self._data_dir / SNAPSHOT_DIR / day, stem, jpeg)

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
        conf: float | None = None
        if confidence is not None:
            conf = float(confidence)
            conf = min(1.0, max(0.0, conf)) if math.isfinite(conf) else None
        iso = _as_iso(ts) or to_iso(self._clock())
        rel = self._rel(snapshot_path) if snapshot_path else None
        caption_s, reason_s = _clip(caption, MAX_CAPTION_LEN), _clip(reason, MAX_REASON_LEN)
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO events(rule_id, ts, caption, reason, confidence, source, "
                "snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, iso, caption_s, reason_s, conf, _clip(source, 256), rel),
            )
            event_id = int(cur.lastrowid)
            if self._fts:
                conn.execute(
                    "INSERT INTO events_fts(rowid, caption, reason) VALUES (?, ?, ?)",
                    (event_id, caption_s, reason_s),
                )
        record = self.get_event(event_id)
        assert record is not None
        return record

    _EVENT_COLS = (
        "e.id, e.rule_id, e.ts, e.caption, e.reason, e.confidence, e.source, "
        "e.snapshot_path, e.feedback"
    )

    def _event(self, row: tuple[Any, ...]) -> EventRecord:
        snap = self._abs(row[7])
        return EventRecord(
            id=int(row[0]), rule_id=row[1], ts=row[2], caption=row[3], reason=row[4],
            confidence=row[5], source=row[6], snapshot_path=str(snap) if snap else None,
            feedback=row[8],
        )

    def get_event(self, event_id: int) -> EventRecord | None:
        rows = self._read(f"SELECT {self._EVENT_COLS} FROM events e WHERE e.id = ?", (event_id,))
        return self._event(rows[0]) if rows else None

    def last_event(self, rule_id: str) -> EventRecord | None:
        rows = self._read(
            f"SELECT {self._EVENT_COLS} FROM events e WHERE e.rule_id = ? "
            "ORDER BY e.ts DESC, e.id DESC LIMIT 1",
            (rule_id,),
        )
        return self._event(rows[0]) if rows else None

    def set_feedback(self, event_id: int, feedback: str | None) -> EventRecord | None:
        """Mark an event ``correct`` / ``false_alarm`` (or clear with ``None``)."""
        if feedback is not None and feedback not in FEEDBACK_VALUES:
            raise ValueError(f"feedback must be one of {FEEDBACK_VALUES} or None")
        with self._tx() as conn:
            n = conn.execute(
                "UPDATE events SET feedback = ? WHERE id = ?", (feedback, event_id)
            ).rowcount
        return self.get_event(event_id) if n else None

    def count_events(self, *, rule_id: str | None = None, since: Any = None) -> int:
        sql, params = "SELECT COUNT(*) FROM events WHERE 1=1", []
        if rule_id is not None:
            sql += " AND rule_id = ?"
            params.append(rule_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(_as_iso(since))
        return int(self._read(sql, params)[0][0])

    @staticmethod
    def _terms(text: str) -> list[str]:
        words = [w.lower() for w in _TERM_RE.findall(text or "")]
        terms = [w for w in words if w not in _STOPWORDS and len(w) > 1]
        # Stopword-only queries ("what happened?") give no terms: the caller returns nothing
        # rather than every event containing "the".
        return list(dict.fromkeys(terms))[:16]

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
        """Filter events. ``text`` matches any meaningful word of the caption/reason (FTS5
        prefix match ranked by relevance, else ``LIKE`` newest first). Without ``text``
        results are newest first. ``since`` is inclusive, ``until`` exclusive."""
        limit = max(1, min(int(limit), MAX_QUERY_LIMIT))
        where, params = ["1=1"], []
        if rule_id is not None:
            where.append("e.rule_id = ?")
            params.append(rule_id)
        if since is not None:
            where.append("e.ts >= ?")
            params.append(_as_iso(since))
        if until is not None:
            where.append("e.ts < ?")
            params.append(_as_iso(until))
        if feedback is not None:
            if feedback == "none":
                where.append("e.feedback IS NULL")
            else:
                where.append("e.feedback = ?")
                params.append(feedback)
        terms = self._terms(text) if text else []
        if text and not terms:
            return []
        if terms and self._fts:
            match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in terms)
            sql = (
                f"SELECT {self._EVENT_COLS} FROM events_fts f JOIN events e ON e.id = f.rowid "
                f"WHERE events_fts MATCH ? AND {' AND '.join(where)} "
                "ORDER BY bm25(events_fts), e.ts DESC LIMIT ?"
            )
            try:
                return [self._event(r) for r in self._read(sql, [match, *params, limit])]
            except sqlite3.OperationalError as exc:
                logger.warning("sentinel: FTS query failed (%s); using LIKE", exc)
        if terms:
            like = []
            for t in terms:
                pattern = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern += "%"
                like.append("(e.caption LIKE ? ESCAPE '\\' OR e.reason LIKE ? ESCAPE '\\')")
                params.extend([pattern, pattern])
            where.append("(" + " OR ".join(like) + ")")
        sql = (
            f"SELECT {self._EVENT_COLS} FROM events e WHERE {' AND '.join(where)} "
            "ORDER BY e.ts DESC, e.id DESC LIMIT ?"
        )
        return [self._event(r) for r in self._read(sql, [*params, limit])]

    # ---- examples -------------------------------------------------------------------

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
        """Attach a teaching example. Give either ``jpeg`` bytes, an existing ``path`` inside
        the data dir (copied), or an ``event_id`` whose snapshot is copied. Keeps at most
        ``max_examples_per_kind`` per rule and kind (oldest dropped with its file)."""
        if kind not in EXAMPLE_KINDS:
            raise ValueError(f"kind must be one of {EXAMPLE_KINDS}")
        if self.get_rule(rule_id) is None:
            raise StoreError(f"unknown rule {rule_id!r}")
        if jpeg is None:
            src: Path | None = None
            if path is not None:
                src = self._abs(self._rel(path))
            elif event_id is not None:
                ev = self.get_event(event_id)
                if ev is None:
                    raise StoreError(f"unknown event {event_id}")
                src = Path(ev.snapshot_path) if ev.snapshot_path else None
            if src is None:
                raise ValueError("an example needs jpeg bytes, a path or an event with a snapshot")
            try:
                if src.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError(f"image larger than {MAX_IMAGE_BYTES} bytes")
                jpeg = src.read_bytes()
            except OSError as exc:
                raise StoreError(f"cannot read example image {src}: {exc}") from exc
        safe_rule = _SAFE_ID_RE.sub("", rule_id)[:64] or "rule"
        stem = f"{kind}_{to_iso(self._clock()).replace(':', '').replace('.', '')}_" + (
            uuid.uuid4().hex[:6]
        )
        written = self._write_image(self._data_dir / EXAMPLE_DIR / safe_rule, stem, jpeg)
        rel = self._rel(written)
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO examples(rule_id, kind, path, event_id, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rule_id, kind, rel, event_id, _clip(note, 500), to_iso(self._clock())),
                )
                example_id = int(cur.lastrowid)
                stale = conn.execute(
                    "SELECT id, path FROM examples WHERE rule_id = ? AND kind = ? "
                    "ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (rule_id, kind, self.max_examples_per_kind),
                ).fetchall()
                conn.executemany("DELETE FROM examples WHERE id = ?", [(r[0],) for r in stale])
        except BaseException:
            self._unlink(rel)
            raise
        for _, p in stale:
            self._unlink(p)
        rows = self._read(
            "SELECT id, rule_id, kind, path, event_id, note, created_at FROM examples "
            "WHERE id = ?",
            (example_id,),
        )
        return self._example(rows[0])

    def _example(self, row: tuple[Any, ...]) -> ExampleRecord:
        p = self._abs(row[3])
        return ExampleRecord(
            id=int(row[0]), rule_id=row[1], kind=row[2], path=str(p) if p else "",
            event_id=row[4], note=row[5], created_at=row[6],
        )

    def list_examples(
        self, rule_id: str, kind: str | None = None, *, limit: int | None = None
    ) -> list[ExampleRecord]:
        """Newest first."""
        sql = (
            "SELECT id, rule_id, kind, path, event_id, note, created_at FROM examples "
            "WHERE rule_id = ?"
        )
        params: list[Any] = [rule_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [self._example(r) for r in self._read(sql, params)]

    def delete_example(self, example_id: int) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT path FROM examples WHERE id = ?", (example_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM examples WHERE id = ?", (example_id,))
        self._unlink(row[0])
        return True

    # ---- usage ----------------------------------------------------------------------

    def _usage_window(self, now: float) -> tuple[str, str]:
        current = hour_bucket(now)
        day_start = hour_bucket(now - 23 * 3600)
        return current, day_start

    def usage_totals(
        self, now: float | None = None, *, rule_id: str | None = None
    ) -> tuple[int, int]:
        """``(calls this UTC hour, calls in the rolling 24 h of hour buckets)``."""
        now = self._clock() if now is None else now
        current, day_start = self._usage_window(now)
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN bucket = ? THEN vision_calls END), 0), "
            "COALESCE(SUM(vision_calls), 0) FROM usage WHERE bucket >= ? AND bucket <= ?"
        )
        params: list[Any] = [current, day_start, current]
        if rule_id is not None:
            sql += " AND rule_id = ?"
            params.append(rule_id)
        row = self._read(sql, params)[0]
        return int(row[0]), int(row[1])

    def usage_buckets(self, now: float | None = None) -> list[tuple[float, int]]:
        """``(bucket start epoch, calls)`` for each non-empty hour bucket in the rolling
        24 h window, oldest first."""
        now = self._clock() if now is None else now
        current, day_start = self._usage_window(now)
        rows = self._read(
            "SELECT bucket, SUM(vision_calls) FROM usage WHERE bucket >= ? AND bucket <= ? "
            "GROUP BY bucket ORDER BY bucket",
            [day_start, current],
        )
        out: list[tuple[float, int]] = []
        for bucket, calls in rows:
            try:
                start = datetime.strptime(str(bucket), "%Y-%m-%dT%H").replace(tzinfo=UTC)
            except ValueError:
                continue
            if int(calls) > 0:
                out.append((start.timestamp(), int(calls)))
        return out

    def try_consume_usage(
        self,
        n: int = 1,
        *,
        hour_cap: int | None,
        day_cap: int | None,
        rule_id: str = "",
        now: float | None = None,
    ) -> UsageResult:
        """Atomically check the caps and, if both allow ``n`` more calls, record them.
        ``None`` means no cap. Totals are across all rules."""
        n = int(n)
        if n < 0:
            raise ValueError("n must be >= 0")
        now = self._clock() if now is None else now
        current, day_start = self._usage_window(now)
        with self._tx() as conn:
            hour_used, day_used = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN bucket = ?2 THEN vision_calls END), 0), "
                "COALESCE(SUM(vision_calls), 0) FROM usage WHERE bucket >= ?1 AND bucket <= ?2",
                (day_start, current),
            ).fetchone()
            hour_used, day_used = int(hour_used), int(day_used)
            if (hour_cap is not None and hour_used + n > hour_cap) or (
                day_cap is not None and day_used + n > day_cap
            ):
                return UsageResult(False, hour_used, day_used)
            if n:
                conn.execute(
                    "INSERT INTO usage(bucket, rule_id, vision_calls) VALUES (?, ?, ?) "
                    "ON CONFLICT(bucket, rule_id) DO UPDATE SET "
                    "vision_calls = vision_calls + excluded.vision_calls",
                    (current, rule_id, n),
                )
        return UsageResult(True, hour_used + n, day_used + n)

    # ---- retention ------------------------------------------------------------------

    def storage_bytes(self) -> int:
        """Bytes used by snapshot and example images."""
        total = 0
        for sub in (SNAPSHOT_DIR, EXAMPLE_DIR):
            root = self._data_dir / sub
            if not root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        total += os.stat(os.path.join(dirpath, name)).st_size
                    except OSError:
                        continue
        return total

    def _snapshot_files(self) -> list[tuple[str, Path, int, float]]:
        """``(day, path, size, mtime)`` for every snapshot file, oldest first."""
        out: list[tuple[str, Path, int, float]] = []
        root = self._data_dir / SNAPSHOT_DIR
        if not root.is_dir():
            return out
        for day_dir in root.iterdir():
            if not day_dir.is_dir():
                continue
            for f in day_dir.iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                if f.is_file():
                    out.append((day_dir.name, f, st.st_size, st.st_mtime))
        out.sort(key=lambda t: (t[0], t[1].name))
        return out

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
        cutoff = to_iso(now - self._retention_days * 86400)
        with self._tx() as conn:
            old = conn.execute(
                "SELECT id, snapshot_path FROM events WHERE ts < ?", (cutoff,)
            ).fetchall()
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            if self._fts and old:
                conn.executemany("DELETE FROM events_fts WHERE rowid = ?", [(r[0],) for r in old])
            usage_cut = hour_bucket(now - USAGE_KEEP_DAYS * 86400)
            report.usage_rows_deleted = conn.execute(
                "DELETE FROM usage WHERE bucket < ?", (usage_cut,)
            ).rowcount
        report.events_deleted = len(old)
        for _, snap in old:
            freed = self._unlink(snap)
            if freed:
                report.snapshots_deleted += 1
                report.bytes_freed += freed

        referenced = {
            r[0] for r in self._read("SELECT snapshot_path FROM events WHERE snapshot_path "
                                     "IS NOT NULL")
        }
        files = self._snapshot_files()
        kept: list[tuple[str, Path, int, float]] = []
        for day, f, size, mtime in files:
            rel = self._rel(f)
            is_tmp = f.name.startswith(".") and f.name.endswith(".tmp")
            if rel not in referenced and (is_tmp or now - mtime > 3600):
                freed = self._unlink(rel)
                report.orphans_deleted += 1
                report.bytes_freed += freed
            else:
                kept.append((day, f, size, mtime))

        cap = int(self._max_storage_mb * 1024 * 1024)
        total = self.storage_bytes()
        victims: list[str] = []
        for _day, f, size, _mtime in kept:
            if total <= cap:
                break
            rel = self._rel(f)
            freed = self._unlink(rel)
            if freed or not f.exists():
                total -= size
                report.bytes_freed += freed
                report.snapshots_deleted += 1
                victims.append(rel)
        if victims:
            with self._tx() as conn:
                conn.executemany(
                    "UPDATE events SET snapshot_path = NULL WHERE snapshot_path = ?",
                    [(v,) for v in victims],
                )
        root = self._data_dir / SNAPSHOT_DIR
        if root.is_dir():
            for day_dir in root.iterdir():
                try:
                    if day_dir.is_dir() and not any(day_dir.iterdir()):
                        day_dir.rmdir()
                except OSError:
                    continue
        report.bytes_after = self.storage_bytes()
        with self._lock:
            self._last_prune = now
        return report

    def prune_due(
        self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS
    ) -> bool:
        """True if :meth:`prune_if_due` would run a prune now."""
        now = self._clock() if now is None else now
        with self._lock:
            return self._last_prune is None or now - self._last_prune >= interval

    def prune_if_due(
        self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS
    ) -> PruneReport | None:
        """Run :meth:`prune` if it has not run for ``interval`` seconds (hourly by default)."""
        now = self._clock() if now is None else now
        with self._lock:
            if self._last_prune is not None and now - self._last_prune < interval:
                return None
        return self.prune(now)


def day_range(day: datetime) -> tuple[str, str]:
    """``(since, until)`` ISO bounds for the UTC day containing ``day``."""
    start = day.astimezone(UTC) if day.tzinfo else day.replace(tzinfo=UTC)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start), to_iso(start + timedelta(days=1))
