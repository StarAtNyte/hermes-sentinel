"""The rolling-hour cap on second opinions."""

from __future__ import annotations

import threading

from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

HourlyCap = plugin_module("sentinel.judging.hourly_cap").HourlyCap


def test_allows_up_to_the_limit_then_refuses():
    cap = HourlyCap(2, clock=FakeClock(0.0))
    assert [cap.try_acquire() for _ in range(3)] == [True, True, False]
    assert cap.used() == 2


def test_slots_free_up_after_an_hour():
    clock = FakeClock(0.0)
    cap = HourlyCap(1, clock=clock)
    assert cap.try_acquire() and not cap.try_acquire()
    clock._now = 3599.0
    assert not cap.try_acquire()
    clock._now = 3600.0
    assert cap.try_acquire()


def test_release_gives_back_the_newest_slot():
    cap = HourlyCap(1, clock=FakeClock(0.0))
    assert cap.try_acquire()
    cap.release()
    assert cap.used() == 0 and cap.try_acquire()
    HourlyCap(1, clock=FakeClock(0.0)).release()  # nothing to give back: harmless


def test_zero_or_negative_limit_allows_nothing():
    assert not HourlyCap(0, clock=FakeClock(0.0)).try_acquire()
    assert not HourlyCap(-3, clock=FakeClock(0.0)).try_acquire()


def test_concurrent_acquisitions_never_exceed_the_limit():
    cap = HourlyCap(5, clock=FakeClock(0.0))
    wins: list[bool] = []
    barrier = threading.Barrier(16)

    def grab():
        barrier.wait()
        wins.append(cap.try_acquire())

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sum(wins) == 5 and cap.used() == 5
