"""Schedule windows."""

from __future__ import annotations

from datetime import datetime

import pytest
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")
RuleSpec = rules.RuleSpec
Schedule = rules.Schedule
RVE = rules.RuleValidationError


def at(day, hh, mm=0):
    # 2026-11-02 is a Monday.
    return datetime(2026, 11, 2 + day, hh, mm)


def test_schedule_daytime_window():
    s = Schedule(start="08:00", end="17:00", days=(0, 1, 2, 3, 4))
    assert s.is_active(at(0, 8))
    assert s.is_active(at(0, 16, 59))
    assert not s.is_active(at(0, 17))
    assert not s.is_active(at(0, 7, 59))
    assert not s.is_active(at(5, 12))  # Saturday


def test_schedule_overnight_window_belongs_to_start_day():
    s = Schedule(start="22:00", end="06:00", days=(4,))  # Friday night
    assert s.is_active(at(4, 23))
    assert s.is_active(at(5, 5, 59))  # early Saturday = Friday's window
    assert not s.is_active(at(5, 6))
    assert not s.is_active(at(5, 23))
    assert not s.is_active(at(4, 3))  # early Friday belongs to Thursday


def test_schedule_overnight_wraps_week():
    s = Schedule(start="22:00", end="02:00", days=(6,))  # Sunday night
    assert s.is_active(at(7, 1))  # Monday 01:00


def test_schedule_all_day_and_24():
    assert Schedule().is_active(at(3, 0))
    assert Schedule(start="09:00", end="09:00", days=(2,)).is_active(at(2, 3))
    assert not Schedule(start="09:00", end="09:00", days=(2,)).is_active(at(3, 3))
    assert Schedule(start="20:00", end="24:00").is_active(at(1, 23, 59))


@pytest.mark.parametrize(
    "kw",
    [
        {"start": "8"},
        {"start": "24:00"},
        {"end": "24:01"},
        {"start": "12:60"},
        {"days": ()},
        {"days": (7,)},
        {"days": ("someday",)},
        {"days": (True,)},
        {"start": None},
    ],
)
def test_schedule_invalid(kw):
    with pytest.raises(RVE):
        Schedule(**kw)


def test_schedule_from_dict_variants():
    assert Schedule.from_dict({"days": None}).days == tuple(range(7))
    assert Schedule.from_dict({"days": "sat"}).days == (5,)
    assert Schedule.from_dict({"days": ["3"]}).days == (3,)
    sch = Schedule(start="01:00")
    assert Schedule.from_dict(sch) is sch
    with pytest.raises(RVE):
        Schedule.from_dict("nightly")
    with pytest.raises(RVE):
        Schedule.from_dict({"days": 3.5})


def test_schedule_round_trip():
    s = Schedule(start="07:30", end="19:15", days=(6, 5))
    assert Schedule.from_dict(s.to_dict()) == s


# --------------------------------------------------------------------------- fallback
