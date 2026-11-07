"""Schema, forward-only migrations and the optional FTS5 index."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from .errors import StoreError
from .records import to_iso

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)

MIGRATION_1_SQL = """
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


def run_statements(conn: sqlite3.Connection, script: str) -> None:
    """Run ``;``-separated DDL one statement at a time.

    (``executescript`` would COMMIT the
    surrounding migration transaction.) Statements here never contain a literal ``;``.
    """
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def migration_1(conn: sqlite3.Connection) -> None:
    """Initial schema: rules, events, examples, usage."""
    run_statements(conn, MIGRATION_1_SQL)


MIGRATION_2_SQL = """
        ALTER TABLE events ADD COLUMN delivery TEXT;
        ALTER TABLE events ADD COLUMN delivery_detail TEXT NOT NULL DEFAULT '';
        ALTER TABLE events ADD COLUMN delivery_ts TEXT
        """


def migration_2(conn: sqlite3.Connection) -> None:
    """Events remember how their alert was delivered: outcome, reason and time."""
    run_statements(conn, MIGRATION_2_SQL)


#: Ordered (version, migration). Append new versions; never edit a shipped one.
MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, migration_1),
    (2, migration_2),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(db: Database, clock: Callable[[], float]) -> None:
    """Bring the database up to :data:`SCHEMA_VERSION`; refuse a newer one."""
    conn = db.conn
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    current = int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"sentinel database schema v{current} is newer than this plugin supports "
            f"(v{SCHEMA_VERSION}); update hermes-sentinel"
        )
    for version, step in MIGRATIONS:
        if version <= current:
            continue
        with db.tx() as tx_conn:
            step(tx_conn)
            tx_conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, to_iso(clock())),
            )
        logger.info("sentinel store migrated to schema v%d", version)


def setup_fts(db: Database) -> bool:
    """Create/repair the FTS5 index. Returns False (LIKE fallback) when unavailable."""
    try:
        with db.tx() as conn:
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
