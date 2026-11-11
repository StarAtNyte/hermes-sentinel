"""The SQLite connection, its single lock and transaction helpers."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import schema
from .errors import StoreError

logger = logging.getLogger(__name__)


def _default_connect(name: str, filename: str) -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db

    return plugin_db(name, filename)


def _default_data_dir(name: str) -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir(name))


class Database:
    """One SQLite connection guarded by one re-entrant lock; owns schema setup."""

    def __init__(
        self,
        *,
        plugin_name: str,
        db_filename: str,
        connect: Callable[[str, str], sqlite3.Connection] | None,
        data_dir: Path | str | None,
        enable_fts: bool,
        clock: Callable[[], float],
    ) -> None:
        """Open the connection, create the data directory and run migrations."""
        self._lock = threading.RLock()  # guards the connection and the closed flag
        self._closed = False
        self._fts = False
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
            schema.migrate(self, clock)
            self._fts = schema.setup_fts(self) if enable_fts else False
        except BaseException:
            self._conn.close()
            raise

    @property
    def lock(self) -> threading.RLock:
        """The lock repositories take when they need a read-modify-write span."""
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        """The raw connection (callers must hold :attr:`lock`, or be single-threaded setup)."""
        return self._conn

    @property
    def data_dir(self) -> Path:
        """Resolved plugin data directory."""
        return self._data_dir

    @property
    def fts_enabled(self) -> bool:
        """Whether the FTS5 index is in use."""
        return self._fts

    def check_open(self) -> None:
        """Raise :class:`StoreError` once the database is closed."""
        if self._closed:
            raise StoreError("store is closed")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` (rollback on error) under the lock."""
        with self._lock:
            self.check_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def read(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a query under the lock and return every row."""
        with self._lock:
            self.check_open()
            return self._conn.execute(sql, params).fetchall()

    def schema_version(self) -> int:
        """Highest applied migration."""
        with self._lock:
            self.check_open()
            row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._conn.close()
                except sqlite3.Error:  # pragma: no cover - close rarely fails
                    logger.debug("sentinel store close failed", exc_info=True)
