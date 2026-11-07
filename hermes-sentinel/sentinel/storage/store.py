"""The :class:`Store` facade: one object composed of small repositories."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..rules import RuleSpec
from .constants import (
    DB_FILENAME,
    DEFAULT_MAX_EXAMPLES_PER_KIND,
    PLUGIN_NAME,
    PRUNE_INTERVAL_SECONDS,
)
from .database import Database
from .errors import StoreError
from .files import ImageFiles
from .records import EventRecord, ExampleRecord, PruneReport, UsageResult
from .repositories import (
    EventRepository,
    ExampleRepository,
    RuleRepository,
    UsageRepository,
)
from .retention import RetentionPolicy, RetentionService


class Store:
    """SQLite-backed persistence for Sentinel.

    Create once per plugin load; ``close()`` on unload. All public methods are thread-safe.
    """

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
        """Validate settings, open the database and build the repositories."""
        try:
            policy = RetentionPolicy(retention_days, max_storage_mb)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"invalid sentinel store settings: {exc}") from exc
        policy.max_examples_per_kind = max_examples_per_kind
        self._policy = policy
        self._database = Database(
            plugin_name=plugin_name,
            db_filename=db_filename,
            connect=connect,
            data_dir=data_dir,
            enable_fts=enable_fts,
            clock=clock,
        )
        self._images = ImageFiles(self._database)
        self._rules = RuleRepository(self._database, self._images)
        self._events = EventRepository(self._database, self._images, clock)
        self._examples = ExampleRepository(
            self._database, self._images, clock, policy, self._rules, self._events
        )
        self._usage = UsageRepository(self._database, clock)
        self._retention = RetentionService(self._database, self._images, policy, clock)

    @property
    def data_dir(self) -> Path:
        """Resolved plugin data directory."""
        return self._database.data_dir

    @property
    def fts_enabled(self) -> bool:
        """Whether the FTS5 index is in use."""
        return self._database.fts_enabled

    @property
    def retention_days(self) -> float:
        """Days events are kept."""
        return self._policy.retention_days

    @retention_days.setter
    def retention_days(self, value: float) -> None:
        """Set retention days."""
        self._policy.retention_days = value

    @property
    def max_storage_mb(self) -> float:
        """Image storage cap in MB."""
        return self._policy.max_storage_mb

    @max_storage_mb.setter
    def max_storage_mb(self, value: float) -> None:
        """Set max storage mb."""
        self._policy.max_storage_mb = value

    @property
    def max_examples_per_kind(self) -> int:
        """Examples kept per rule and kind."""
        return self._policy.max_examples_per_kind

    @max_examples_per_kind.setter
    def max_examples_per_kind(self, value: int) -> None:
        """Set max examples per kind."""
        self._policy.max_examples_per_kind = value

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        self._database.close()

    def __enter__(self) -> Store:
        """Context-manager entry."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close on context exit."""
        self.close()

    def schema_version(self) -> int:
        """Highest applied migration."""
        return self._database.schema_version()

    def upsert_rule(self, spec: RuleSpec) -> RuleSpec:
        """Insert or replace a rule (validated first)."""
        return self._rules.upsert_rule(spec)

    def get_rule(self, rule_id: str) -> RuleSpec | None:
        """The rule with ``rule_id``, or ``None``."""
        return self._rules.get_rule(rule_id)

    def list_rules(self, *, enabled_only: bool = False) -> list[RuleSpec]:
        """All readable rules, oldest first."""
        return self._rules.list_rules(enabled_only=enabled_only)

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> RuleSpec | None:
        """Pause/resume. Returns the updated spec, or ``None`` if the rule does not exist."""
        return self._rules.set_rule_enabled(rule_id, enabled)

    def delete_rule(self, rule_id: str, *, purge_events: bool = False) -> bool:
        """Delete a rule and its examples; events too when ``purge_events``."""
        return self._rules.delete_rule(rule_id, purge_events=purge_events)

    def save_snapshot(
        self, jpeg: bytes, *, ts: datetime | float | None = None, rule_id: str = ""
    ) -> Path:
        """Write a JPEG under ``snapshots/YYYY-MM-DD/`` atomically; returns its path."""
        return self._events.save_snapshot(jpeg, ts=ts, rule_id=rule_id)

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
        return self._events.add_event(
            rule_id,
            caption=caption,
            reason=reason,
            confidence=confidence,
            source=source,
            snapshot_path=snapshot_path,
            ts=ts,
        )

    def get_event(self, event_id: int) -> EventRecord | None:
        """The event with ``event_id``, or ``None``."""
        return self._events.get_event(event_id)

    def last_event(self, rule_id: str) -> EventRecord | None:
        """The newest event for ``rule_id``, or ``None``."""
        return self._events.last_event(rule_id)

    def set_feedback(self, event_id: int, feedback: str | None) -> EventRecord | None:
        """Mark an event ``correct`` / ``false_alarm`` (or clear with ``None``)."""
        return self._events.set_feedback(event_id, feedback)

    def set_delivery(self, event_id: int, outcome: str, detail: str = "") -> EventRecord | None:
        """Record how an event's alert was delivered, with the time."""
        return self._events.set_delivery(event_id, outcome, detail)

    def count_events(self, *, rule_id: str | None = None, since: Any = None) -> int:
        """Number of events, optionally for one rule and/or since a time."""
        return self._events.count_events(rule_id=rule_id, since=since)

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
        """Filter events by text, rule, time range (``until`` exclusive), feedback, limit."""
        return self._events.query_events(
            text=text, rule_id=rule_id, since=since, until=until, feedback=feedback, limit=limit
        )

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
        """Attach a teaching example from ``jpeg`` bytes, a data-dir ``path`` or an event."""
        return self._examples.add_example(
            rule_id, kind, jpeg, path=path, event_id=event_id, note=note
        )

    def list_examples(
        self, rule_id: str, kind: str | None = None, *, limit: int | None = None
    ) -> list[ExampleRecord]:
        """Examples for a rule, newest first."""
        return self._examples.list_examples(rule_id, kind, limit=limit)

    def delete_example(self, example_id: int) -> bool:
        """Delete one example row and its file."""
        return self._examples.delete_example(example_id)

    def usage_totals(
        self, now: float | None = None, *, rule_id: str | None = None
    ) -> tuple[int, int]:
        """``(calls this UTC hour, calls in the rolling 24 h of hour buckets)``."""
        return self._usage.usage_totals(now, rule_id=rule_id)

    def usage_buckets(self, now: float | None = None) -> list[tuple[float, int]]:
        """``(bucket start epoch, calls)`` per non-empty hour in the rolling window."""
        return self._usage.usage_buckets(now)

    def try_consume_usage(
        self,
        n: int = 1,
        *,
        hour_cap: int | None,
        day_cap: int | None,
        rule_id: str = "",
        now: float | None = None,
    ) -> UsageResult:
        """Atomically check the caps and, if both allow ``n`` more calls, record them."""
        return self._usage.try_consume_usage(
            n, hour_cap=hour_cap, day_cap=day_cap, rule_id=rule_id, now=now
        )

    def storage_bytes(self) -> int:
        """Bytes used by snapshot and example images."""
        return self._images.storage_bytes()

    def prune(self, now: float | None = None) -> PruneReport:
        """Apply retention and the storage cap."""
        return self._retention.prune(now)

    def prune_due(self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS) -> bool:
        """True if :meth:`prune_if_due` would run a prune now."""
        return self._retention.prune_due(now, interval)

    def prune_if_due(
        self, now: float | None = None, interval: float = PRUNE_INTERVAL_SECONDS
    ) -> PruneReport | None:
        """Run :meth:`prune` if it has not run for ``interval`` seconds."""
        return self._retention.prune_if_due(now, interval)
