"""Persistent state: rules, events, teaching examples, usage counters and snapshot images.

Everything lives under the plugin's Hermes data directory:

* ``plugin_db("sentinel-vision", "sentinel.db")`` - SQLite (WAL) with the tables below.
* ``plugin_data_dir("sentinel-vision")/snapshots/YYYY-MM-DD/*.jpg`` - event snapshots.
* ``plugin_data_dir("sentinel-vision")/examples/<rule_id>/*.jpg`` - teaching examples.

Schema (version tracked in ``schema_migrations``; migrations run forward on open)::

    rules     (id PK, spec_json, enabled, created_at, updated_at)
    events    (id PK, rule_id, ts, caption, reason, confidence, source, snapshot_path, feedback,
                delivery, delivery_detail, delivery_ts)
    examples  (id PK, rule_id -> rules ON DELETE CASCADE, kind pos|neg, path, event_id, ...)
    usage     (bucket 'YYYY-MM-DDTHH' UTC, rule_id, vision_calls)  PK(bucket, rule_id)
    events_fts  FTS5(caption, reason), rowid = events.id   (only when FTS5 is available)

``ts`` columns are ISO-8601 UTC with a fixed width (``2026-11-06T09:12:03.120Z``) so string
order is time order. Paths are stored relative to the data directory and returned absolute.
Free-text search uses FTS5 when the SQLite build has it and falls back to ``LIKE`` otherwise.

Thread safety: one connection (``check_same_thread=False``) guarded by a re-entrant lock owned
by :class:`Database`; writes use ``BEGIN IMMEDIATE`` so another process (the dashboard) can
share the file. :class:`Store` is a thin facade over the repositories.
"""

from __future__ import annotations

from .constants import (
    DB_FILENAME,
    DEFAULT_MAX_EXAMPLES_PER_KIND,
    EXAMPLE_DIR,
    EXAMPLE_KINDS,
    FEEDBACK_VALUES,
    MAX_CAPTION_LEN,
    MAX_IMAGE_BYTES,
    MAX_QUERY_LIMIT,
    MAX_REASON_LEN,
    PLUGIN_NAME,
    PRUNE_INTERVAL_SECONDS,
    SNAPSHOT_DIR,
    USAGE_KEEP_DAYS,
)
from .errors import StoreError
from .records import (
    EventRecord,
    ExampleRecord,
    PruneReport,
    UsageResult,
    day_range,
    hour_bucket,
    parse_ts,
    to_iso,
)
from .schema import MIGRATIONS, SCHEMA_VERSION
from .store import Store

__all__ = [
    "DB_FILENAME",
    "DEFAULT_MAX_EXAMPLES_PER_KIND",
    "EXAMPLE_DIR",
    "EXAMPLE_KINDS",
    "FEEDBACK_VALUES",
    "MAX_CAPTION_LEN",
    "MAX_IMAGE_BYTES",
    "MAX_QUERY_LIMIT",
    "MAX_REASON_LEN",
    "MIGRATIONS",
    "PLUGIN_NAME",
    "PRUNE_INTERVAL_SECONDS",
    "SCHEMA_VERSION",
    "SNAPSHOT_DIR",
    "USAGE_KEEP_DAYS",
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
