"""Vision-call usage counters (hour buckets, UTC)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..database import Database
from ..records import UsageResult, hour_bucket


class UsageRepository:
    """Counts vision calls per UTC hour and answers rolling-window questions."""

    def __init__(self, database: Database, clock: Callable[[], float]) -> None:
        """Bind to the shared database and clock."""
        self._db = database
        self._clock = clock

    @staticmethod
    def _usage_window(now: float) -> tuple[str, str]:
        return hour_bucket(now), hour_bucket(now - 23 * 3600)

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
        row = self._db.read(sql, params)[0]
        return int(row[0]), int(row[1])

    def usage_buckets(self, now: float | None = None) -> list[tuple[float, int]]:
        """``(bucket start epoch, calls)`` per non-empty hour bucket.

        Covers the rolling 24 h window, oldest first.
        """
        now = self._clock() if now is None else now
        current, day_start = self._usage_window(now)
        rows = self._db.read(
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

        ``None`` means no cap. Totals are across all rules.
        """
        n = int(n)
        if n < 0:
            raise ValueError("n must be >= 0")
        now = self._clock() if now is None else now
        current, day_start = self._usage_window(now)
        with self._db.tx() as conn:
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
