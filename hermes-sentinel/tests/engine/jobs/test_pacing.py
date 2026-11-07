"""Per-rule pacing: spacing between judge calls, the rolling hourly cap, the one-shot notice."""

from __future__ import annotations

from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

pacing_mod = plugin_module("sentinel.engine.jobs.pacing")


def make(min_interval=10.0, per_hour=3):
    clock = FakeClock(0.0)
    return clock, pacing_mod.RulePacing(min_interval, per_hour, clock=clock)


def test_spacing_blocks_calls_closer_than_the_interval():
    _, pacing = make()
    assert pacing.begin_call(0.0, confirming=False)
    assert not pacing.spaced(9.9)
    assert not pacing.begin_call(9.9, confirming=False)
    assert pacing.begin_call(10.0, confirming=False)


def test_confirmation_looks_skip_the_spacing_but_not_the_cap():
    _, pacing = make(per_hour=2)
    assert pacing.begin_call(0.0, confirming=False)
    assert pacing.begin_call(1.0, confirming=True)
    assert not pacing.begin_call(2.0, confirming=True)


def test_cap_frees_as_the_hour_rolls_on():
    clock, pacing = make(min_interval=0.0, per_hour=2)
    assert pacing.begin_call(0.0, confirming=False) and pacing.begin_call(1.0, confirming=False)
    assert not pacing.has_room() and pacing.used() == 2
    clock.advance(3600.0)
    assert pacing.has_room() and pacing.used() == 0


def test_cap_notice_is_given_once_until_a_call_is_made_again():
    clock, pacing = make(min_interval=0.0, per_hour=1)
    pacing.begin_call(0.0, confirming=False)
    assert pacing.take_cap_notice() and not pacing.take_cap_notice()
    clock.advance(3600.0)
    assert pacing.begin_call(3600.0, confirming=False)
    assert pacing.take_cap_notice()


def test_cancelled_call_gives_the_slot_back():
    _, pacing = make(per_hour=1)
    pacing.begin_call(0.0, confirming=False)
    pacing.cancel_call()
    assert pacing.has_room()
