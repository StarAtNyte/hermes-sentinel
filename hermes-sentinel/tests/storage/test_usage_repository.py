"""Usage counters."""

from __future__ import annotations

import threading

import pytest
from harness.store_helpers import store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- usage


def test_usage_caps(store, clock):
    for _ in range(3):
        assert store.try_consume_usage(hour_cap=3, day_cap=10, rule_id="ra").allowed
    r = store.try_consume_usage(hour_cap=3, day_cap=10, rule_id="rb")
    assert (r.allowed, r.hour_used, r.day_used) == (False, 3, 3)
    clock.t += 3600
    r = store.try_consume_usage(2, hour_cap=3, day_cap=10, rule_id="rb")
    assert (r.allowed, r.hour_used, r.day_used) == (True, 2, 5)
    assert store.usage_totals() == (2, 5)
    assert store.usage_totals(rule_id="ra") == (0, 3)
    assert store.usage_totals(rule_id="rb") == (2, 2)


def test_usage_day_cap_rolls_hourly(store, clock):
    assert store.try_consume_usage(5, hour_cap=None, day_cap=5).allowed
    clock.t += 23 * 3600
    assert not store.try_consume_usage(hour_cap=None, day_cap=5).allowed
    clock.t += 3600  # first bucket now outside the 24 h window
    assert store.try_consume_usage(hour_cap=None, day_cap=5).allowed
    assert store.usage_totals() == (1, 1)


def test_usage_unlimited_zero_and_negative(store):
    assert store.try_consume_usage(1000, hour_cap=None, day_cap=None).allowed
    assert not store.try_consume_usage(hour_cap=0, day_cap=None).allowed
    assert not store.try_consume_usage(0, hour_cap=0, day_cap=None).allowed  # already over
    with pytest.raises(ValueError):
        store.try_consume_usage(-1, hour_cap=None, day_cap=None)


def test_usage_zero_calls_fit_an_unused_zero_cap(store):
    r = store.try_consume_usage(0, hour_cap=0, day_cap=0)
    assert r.allowed and store.usage_totals() == (0, 0)


def test_usage_persists(plugin_data_dir, clock):
    s = Store(clock=clock)
    s.try_consume_usage(4, hour_cap=None, day_cap=None)
    s.close()
    s = Store(clock=clock)
    assert s.usage_totals() == (4, 4)
    s.close()


def test_usage_concurrent_never_exceeds_cap(store):
    allowed = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            ok = store.try_consume_usage(hour_cap=50, day_cap=None).allowed
            with lock:
                allowed.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 50
    assert store.usage_totals()[0] == 50


def test_usage_concurrent_two_connections(plugin_data_dir, clock):
    a, b = Store(clock=clock), Store(clock=clock)
    results = []

    def worker(s):
        for _ in range(15):
            results.append(s.try_consume_usage(hour_cap=20, day_cap=None).allowed)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 20
    a.close()
    b.close()
