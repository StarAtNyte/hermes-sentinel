"""Budget: hour/day caps persisted in the store, one exhaustion notice per episode."""

from __future__ import annotations

import threading

import pytest
from harness.plugin_loader import plugin_module

budget_mod = plugin_module("sentinel.budget")
store_mod = plugin_module("sentinel.store")
Budget = budget_mod.Budget

T0 = 1_762_430_400.0  # top of an hour (12:00:00Z)


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    yield s
    s.close()


def test_defaults(store, clock):
    b = Budget(store, clock=clock)
    assert (b.per_hour, b.per_day) == (60, 500)
    st = b.status()
    assert (st.hour_used, st.day_used, st.exhausted) == (0, 0, False)
    assert st.hour_remaining == 60 and st.day_remaining == 500
    assert st.retry_after_seconds == 0


def test_hour_cap(store, clock):
    b = Budget(store, per_hour=3, per_day=100, clock=clock)
    assert [b.try_consume(rule_id="r") for _ in range(4)] == [True, True, True, False]
    st = b.status()
    assert st.exhausted and st.hour_used == 3 and st.hour_remaining == 0
    clock.t += 1800
    assert b.status().retry_after_seconds == pytest.approx(1800)
    assert not b.try_consume()
    clock.t += 1800  # next hour
    assert b.try_consume()


def test_day_cap(store, clock):
    b = Budget(store, per_hour=None, per_day=5, clock=clock)
    assert b.try_consume(5)
    clock.t += 5 * 3600
    assert not b.try_consume()
    assert b.status().day_remaining == 0
    clock.t += 19 * 3600  # first bucket rolls out
    assert b.try_consume()


def test_multi_call_reservation_all_or_nothing(store, clock):
    b = Budget(store, per_hour=5, per_day=None, clock=clock)
    assert b.try_consume(4)
    assert not b.try_consume(2)
    assert b.status().hour_used == 4
    assert b.try_consume(1)


def test_unlimited_and_zero(store, clock):
    assert Budget(store, per_hour=None, per_day=None, clock=clock).try_consume(10_000)
    zero = Budget(store, per_hour=0, per_day=None, clock=clock)
    assert not zero.try_consume()
    assert zero.status().exhausted
    st = Budget(store, per_hour=None, per_day=None, clock=clock).status()
    assert st.hour_remaining is None and not st.exhausted


def test_persisted_across_instances(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    Budget(s, per_hour=3, clock=clock).try_consume(3)
    s.close()
    s = store_mod.Store(clock=clock)
    b = Budget(s, per_hour=3, clock=clock)
    assert not b.try_consume()
    assert b.status().hour_used == 3
    s.close()


def test_set_caps(store, clock):
    b = Budget(store, per_hour=1, clock=clock)
    assert b.try_consume() and not b.try_consume()
    b.set_caps(per_hour=2, per_day=None)
    assert b.try_consume()
    for bad in (-1, 1.5, True, "5"):
        with pytest.raises(ValueError):
            b.set_caps(per_hour=bad, per_day=None)
    with pytest.raises(ValueError):
        Budget(store, per_day=-3, clock=clock)


def test_notice_once_per_episode(store, clock):
    b = Budget(store, per_hour=1, per_day=None, clock=clock)
    assert b.take_exhausted_notice() is None
    b.try_consume()
    assert b.take_exhausted_notice() is None
    b.try_consume()
    b.try_consume()
    notice = b.take_exhausted_notice()
    assert notice and "paused" in notice and "1/1 this hour" in notice
    assert "resumes in about 60 min" in notice
    b.try_consume()
    assert b.take_exhausted_notice() is None  # still the same episode
    clock.t += 3600
    assert b.try_consume()  # recovered -> re-armed
    assert b.take_exhausted_notice() is None
    assert not b.try_consume()
    assert b.take_exhausted_notice() is not None


def test_status_dict_and_describe(store, clock):
    b = Budget(store, per_hour=None, per_day=10, clock=clock)
    b.try_consume(2)
    d = b.status().to_dict()
    assert d == {
        "hour_used": 2, "hour_cap": None, "day_used": 2, "day_cap": 10, "exhausted": False,
        "retry_after_seconds": 0.0, "hour_remaining": None, "day_remaining": 8,
    }
    assert b.status().describe() == (
        "Vision checks: 2/unlimited this hour, 2/10 in the last 24 h."
    )


def test_counts_per_rule_but_caps_global(store, clock):
    b = Budget(store, per_hour=2, per_day=None, clock=clock)
    assert b.try_consume(rule_id="a") and b.try_consume(rule_id="b")
    assert not b.try_consume(rule_id="c")
    assert store.usage_totals(rule_id="a") == (1, 1)


def test_backend_returning_bool_supported(clock):
    class Fake:
        def __init__(self):
            self.calls = []

        def try_consume_usage(self, n=1, *, hour_cap, day_cap, rule_id="", now=None):
            self.calls.append((n, hour_cap, day_cap, rule_id, now))
            return False

        def usage_totals(self, now=None, *, rule_id=None):
            return (7, 9)

    fake = Fake()
    b = Budget(fake, per_hour=7, per_day=9, clock=clock)
    assert b.try_consume(rule_id="r") is False
    assert fake.calls == [(1, 7, 9, "r", T0)]
    assert b.status().exhausted


def test_concurrent_consumers(store, clock):
    b = Budget(store, per_hour=25, per_day=None, clock=clock)
    results = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            ok = b.try_consume()
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 25


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 60), (30, 30), (30.9, 30), ("45", 45), (" 12 ", 12), ("unlimited", None),
        ("off", None), ("none", None), ("abc", 60), (-1, 60), (float("nan"), 60),
        (float("inf"), 60), (True, 60), ([], 60), (0, 0), (10**9, budget_mod.MAX_CAP),
    ],
)
def test_parse_cap(value, expected):
    assert budget_mod.parse_cap(value, 60) == expected
