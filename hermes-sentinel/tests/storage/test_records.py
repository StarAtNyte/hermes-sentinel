"""Time helpers and record types."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harness.store_helpers import T0, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- helpers


def test_to_iso_fixed_width_and_parse():
    assert store_mod.to_iso(T0) == "2025-11-06T12:00:00.000Z"
    assert store_mod.to_iso(T0 + 0.1234) == "2025-11-06T12:00:00.123Z"
    naive = datetime(2025, 1, 2, 3, 4, 5)
    assert store_mod.to_iso(naive) == "2025-01-02T03:04:05.000Z"
    assert store_mod.parse_ts("2025-01-02T03:04:05Z") == naive.replace(tzinfo=UTC)
    assert store_mod.parse_ts("2025-01-02T05:04:05+02:00") == naive.replace(tzinfo=UTC)
    assert store_mod.parse_ts("2025-01-02T03:04:05") == naive.replace(tzinfo=UTC)
    assert store_mod.hour_bucket(T0 + 59 * 60) == "2025-11-06T12"
    assert store_mod.to_iso().endswith("Z")
    with pytest.raises(TypeError):
        store_mod.to_iso("now")
    with pytest.raises(ValueError):
        store_mod.to_iso(float("nan"))


def test_day_range():
    since, until = store_mod.day_range(datetime(2025, 11, 6, 15, 30))
    assert (since, until) == ("2025-11-06T00:00:00.000Z", "2025-11-07T00:00:00.000Z")
