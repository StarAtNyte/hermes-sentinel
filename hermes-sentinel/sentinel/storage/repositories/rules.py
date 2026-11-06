"""Rule persistence."""

from __future__ import annotations

import logging
import sqlite3

from ...rules import RuleSpec, RuleValidationError
from ..database import Database
from ..files import ImageFiles

logger = logging.getLogger(__name__)


class RuleRepository:
    """CRUD for the ``rules`` table."""

    def __init__(self, database: Database, images: ImageFiles) -> None:
        """Bind to the shared database and image files."""
        self._db = database
        self._images = images

    def upsert_rule(self, spec: RuleSpec) -> RuleSpec:
        """Insert or replace a rule (validated first)."""
        spec.validate()
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO rules(id, spec_json, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "spec_json=excluded.spec_json, enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (spec.id, spec.to_json(), int(spec.enabled), spec.created_at, spec.updated_at),
            )
        return spec

    @staticmethod
    def _row_to_spec(rule_id: str, spec_json: str) -> RuleSpec | None:
        try:
            return RuleSpec.from_json(spec_json)
        except RuleValidationError as exc:
            logger.warning("sentinel: skipping unreadable rule %s: %s", rule_id, exc)
            return None

    def get_rule(self, rule_id: str) -> RuleSpec | None:
        """The rule with ``rule_id``, or ``None``."""
        rows = self._db.read("SELECT id, spec_json FROM rules WHERE id = ?", (rule_id,))
        return self._row_to_spec(*rows[0]) if rows else None

    def list_rules(self, *, enabled_only: bool = False) -> list[RuleSpec]:
        """All readable rules, oldest first."""
        sql = "SELECT id, spec_json FROM rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at, id"
        rows = (self._row_to_spec(*r) for r in self._db.read(sql))
        return [s for s in rows if s is not None]

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> RuleSpec | None:
        """Pause/resume. Returns the updated spec, or ``None`` if the rule does not exist."""
        with self._db.lock:
            spec = self.get_rule(rule_id)
            if spec is None:
                return None
            return self.upsert_rule(spec.with_changes(enabled=bool(enabled)))

    def delete_rule(self, rule_id: str, *, purge_events: bool = False) -> bool:
        """Delete a rule and its examples (rows and files).

        Events are kept as history unless ``purge_events`` (then their snapshots go too).
        """
        with self._db.tx() as conn:
            rows = conn.execute("SELECT path FROM examples WHERE rule_id = ?", (rule_id,))
            ex_paths = [r[0] for r in rows]
            ev_rows = self._purge_events(conn, rule_id) if purge_events else []
            deleted = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount
        for p in ex_paths:
            self._images.unlink(p)
        for _, snap in ev_rows:
            self._images.unlink(snap)
        return bool(deleted)

    def _purge_events(self, conn: sqlite3.Connection, rule_id: str) -> list[tuple[int, str | None]]:
        """Delete a rule's events (and FTS rows) inside the caller's transaction."""
        ev_rows: list[tuple[int, str | None]] = conn.execute(
            "SELECT id, snapshot_path FROM events WHERE rule_id = ?", (rule_id,)
        ).fetchall()
        conn.execute("DELETE FROM events WHERE rule_id = ?", (rule_id,))
        if self._db.fts_enabled:
            conn.executemany("DELETE FROM events_fts WHERE rowid = ?", [(r[0],) for r in ev_rows])
        return ev_rows
