"""A scene that changes on every frame must not make a rule spend judge calls without bound.

Found in a live webcam test: a person moving in front of the camera kept the change gate firing,
so a fired rule kept being judged inside its cooldown (it stays latched in ``firing``).
"""

from __future__ import annotations

from itertools import groupby

import pytest
from harness.engine_rig import Harness, Verdict, config_mod

FRAME_STEP = 0.5
THIRTY_MINUTES = 1800.0
COOLDOWN = 300.0


@pytest.fixture
def moving_scene(fake_ctx):
    """Factory for a rule on an always-changing source; harnesses are closed after the test."""
    made = []

    def build(**kwargs):
        made.append(build_scene(fake_ctx, **kwargs))
        return made[-1]

    yield build
    for h in made:
        h.engine.stop()
        h.store.close()


def build_scene(ctx, *, positive_at=lambda t: True, **config):
    """A rule on a source that changes every frame; ``positive_at(t)`` decides each verdict."""
    cfg = config_mod.SentinelConfig(batch_judging=False, **config)
    h = Harness(ctx, config=cfg)
    h.add(cooldown_seconds=COOLDOWN)
    h.call_times = []
    original = h.judge.judge

    def judge(frame, question, **kwargs):
        now = h.clock()
        h.call_times.append(now)
        h.judge.default = Verdict(positive_at(now - 100.0), 0.9, caption="hand up")
        return original(frame, question, **kwargs)

    h.judge.judge = judge
    return h


def run(h, seconds=THIRTY_MINUTES):
    start = h.clock()
    while h.clock() - start < seconds:
        h.step(gate_result="change")
        h.clock.advance(FRAME_STEP)


def alerts(h):
    return [m for m in h.ctx.injected if m.content.startswith("[Sentinel alert]")]


def test_no_judge_call_during_cooldown_while_the_scene_keeps_changing(moving_scene):
    h = moving_scene(rule_calls_per_hour=1000)
    run(h)
    fired_at = h.call_times[1]  # the confirmation look that fired the rule
    assert h.call_times[0] == 100.0 and fired_at == 101.0
    in_cooldown = [t for t in h.call_times if fired_at < t < fired_at + COOLDOWN]
    assert in_cooldown == []
    after = [t for t in h.call_times if t >= fired_at + COOLDOWN]
    assert all(b - a >= 60.0 for a, b in zip(after, after[1:], strict=False))
    assert len(h.call_times) == 2 + int((THIRTY_MINUTES - COOLDOWN) // 60)
    assert len(alerts(h)) == 1


def test_hourly_cap_bounds_calls_and_gives_one_notice(moving_scene):
    h = moving_scene(latched_min_interval_seconds=10.0)  # 10 s apart even latched, 30 per hour
    run(h, 800.0)  # long enough for the cap to bind, short enough that evidence is not stale
    assert len(h.call_times) == 30
    assert len(h.notices("judge calls per hour")) == 1
    assert h.engine.rule_status("r1")["state"] == "firing"  # the rule keeps its state
    assert h.engine.rule_status("r1")["judge_calls_hour"] == 30
    assert len(alerts(h)) == 1


def test_bursts_alert_once_per_cooldown_period_and_never_inside_it(moving_scene):
    burst = lambda t: t % 400.0 < 60.0  # noqa: E731 - a person comes by every 400 s
    h = moving_scene(positive_at=burst, rule_calls_per_hour=1000)
    run(h)
    events = h.store.count_events(rule_id="r1")
    assert len(alerts(h)) == events == 5  # bursts at t = 0, 400, 800, 1200, 1600
    positives = [t for t in h.call_times if burst(t - 100.0)]
    for _, group in groupby(positives, key=lambda t: (t - 100.0) // 400.0):
        fired_at = list(group)[1]  # the second positive look confirms and fires
        assert not [t for t in h.call_times if fired_at < t < fired_at + COOLDOWN]


def test_minimum_interval_alone_bounds_a_rule_that_never_fires(moving_scene):
    h = moving_scene(positive_at=lambda t: False, rule_calls_per_hour=1000)
    run(h)
    assert len(h.call_times) == int(THIRTY_MINUTES // 10)


def test_a_change_held_back_by_the_minimum_interval_is_judged_once_it_is_allowed(moving_scene):
    h = moving_scene(positive_at=lambda t: False)
    h.step(gate_result="tick")  # the start-up look
    h.clock.advance(3.0)
    h.step(gate_result="change")  # a real change 3 s later is held back, not lost
    assert h.call_times == [100.0]
    h.clock.advance(6.9)
    h.step()
    assert h.call_times == [100.0]
    h.clock.advance(0.1)
    h.step()
    assert h.call_times == [100.0, 110.0]
    h.clock.advance(20.0)
    h.step()  # nothing is pending any more
    assert len(h.call_times) == 2


def test_latched_rule_is_judged_no_more_than_once_a_minute_and_still_re_arms(moving_scene):
    scene_present = lambda t: t < 700.0  # noqa: E731 - the person leaves at t = 800
    h = moving_scene(positive_at=scene_present, rule_calls_per_hour=1000)
    run(h)
    fired_at = h.call_times[1]
    latched_calls = [t for t in h.call_times if t >= fired_at + COOLDOWN]
    assert latched_calls[0] - (fired_at + COOLDOWN) < 10.0  # the look held back is judged at once
    gone = [t for t in latched_calls if t - 100.0 >= 700.0]
    rearmed_at = gone[1]  # two refuting looks (n - k + 1) re-arm the rule
    latched = [t for t in latched_calls if t <= rearmed_at]
    assert all(b - a >= 60.0 for a, b in zip(latched, latched[1:], strict=False))
    assert len(latched) == 1 + int((rearmed_at - fired_at - COOLDOWN) // 60)
    assert len(alerts(h)) == 1
    assert h.store.count_events(rule_id="r1") == 1
    assert h.engine.rule_status("r1")["state"] != "firing"  # the clear verdict re-armed it


def test_a_shorter_latched_interval_is_honoured(moving_scene):
    h = moving_scene(latched_min_interval_seconds=20.0, rule_calls_per_hour=1000)
    run(h)
    fired_at = h.call_times[1]
    after = [t for t in h.call_times if t >= fired_at + COOLDOWN]
    assert all(b - a >= 20.0 for a, b in zip(after, after[1:], strict=False))
    assert len(h.call_times) == 2 + int((THIRTY_MINUTES - COOLDOWN) // 20)
