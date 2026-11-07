"""RuleStateMachine: k-of-n confirmation, hold, latch, cooldown, staleness, edge cases."""

from __future__ import annotations

import math
import threading

import pytest
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")
RuleSpec = rules.RuleSpec
RuleState = rules.RuleState
SM = rules.RuleStateMachine

H = (True, 0.9)  # confident yes
M = (False, 0.9)  # confident no
LOW = (True, 0.5)  # unsure yes (below the 0.7 threshold)


def make(mode="trigger", *, k=2, n=3, hold=0.0, cooldown=300.0, thr=0.7, stale=900.0, clock=None):
    spec = RuleSpec(
        question="Is the mug on the desk?",
        source="webcam:0",
        id="rtest",
        mode=mode,
        confirm_k=k,
        confirm_n=n,
        hold_seconds=hold,
        cooldown_seconds=cooldown,
        confidence_threshold=thr,
    ).validate()
    kwargs = {"stale_after_seconds": stale}
    if clock is not None:
        kwargs["clock"] = clock
    return SM(spec, **kwargs)


def feed(sm, seq):
    """Feed ``[(t, (match, conf)), ...]``; return list of StepResults."""
    return [sm.observe(v[0], v[1], now=t) for t, v in seq]


# --------------------------------------------------------------------------- trigger basics


def test_starts_idle():
    sm = make()
    assert sm.state is RuleState.IDLE
    assert sm.fire_count == 0


def test_trigger_two_of_three_fires_on_second_hit():
    sm = make()
    r1 = sm.observe(*H, now=0)
    assert r1.state is RuleState.CANDIDATE and r1.fired is None and r1.changed
    r2 = sm.observe(*H, now=1)
    assert r2.fired is not None
    assert r2.state is RuleState.FIRING
    ev = r2.fired
    assert ev.rule_id == "rtest" and ev.mode == "trigger"
    assert ev.ts == 1 and ev.truth_since == 0 and ev.held_seconds == 1
    assert ev.hits == 2 and ev.window == 2
    assert ev.confidence == pytest.approx(0.9)


def test_fire_confidence_is_mean_of_hits():
    sm = make()
    sm.observe(True, 0.8, now=0)
    r = sm.observe(True, 1.0, now=1)
    assert r.fired.confidence == pytest.approx(0.9)


def test_single_hit_then_misses_returns_to_idle():
    sm = make()
    sm.observe(*H, now=0)
    r = sm.observe(*M, now=1)
    assert r.state is RuleState.CANDIDATE  # one miss is tolerated (refute needs 2)
    r = sm.observe(*M, now=2)
    assert r.state is RuleState.IDLE and r.fired is None


def test_leading_misses_do_not_refute_a_new_candidate():
    sm = make()
    feed(sm, [(0, M), (1, M)])
    assert sm.state is RuleState.IDLE
    r = sm.observe(*H, now=2)
    assert r.state is RuleState.CANDIDATE
    assert sm.observe(*H, now=3).fired is not None


def test_k_of_n_with_gap():
    sm = make()
    results = feed(sm, [(0, H), (1, M), (2, H)])
    assert [r.fired is not None for r in results] == [False, False, True]


def test_low_confidence_yes_is_a_miss():
    sm = make()
    r = sm.observe(*LOW, now=0)
    assert r.counted and r.state is RuleState.IDLE
    feed(sm, [(1, H), (2, LOW), (3, LOW)])
    assert sm.state is RuleState.IDLE
    assert sm.fire_count == 0


def test_confidence_equal_to_threshold_counts():
    sm = make(thr=0.7)
    feed(sm, [(0, (True, 0.7))])
    assert sm.observe(True, 0.7, now=1).fired is not None


def test_threshold_zero_counts_any_yes():
    sm = make(thr=0.0)
    feed(sm, [(0, (True, 0.0))])
    assert sm.observe(True, 0.0, now=1).fired is not None


def test_latched_while_true_no_repeat_alerts():
    sm = make(cooldown=0)
    feed(sm, [(0, H), (1, H)])
    results = feed(sm, [(t, H) for t in range(2, 50)])
    assert all(r.fired is None for r in results)
    assert sm.state is RuleState.FIRING
    assert sm.fire_count == 1


def test_latch_tolerates_single_miss():
    sm = make()
    feed(sm, [(0, H), (1, H), (2, M), (3, H)])
    assert sm.state is RuleState.FIRING


def test_rearm_into_cooldown_then_idle():
    sm = make(cooldown=300)
    feed(sm, [(0, H), (1, H)])  # fires at t=1, cooldown until 301
    r = feed(sm, [(2, M), (3, M)])[-1]
    assert r.state is RuleState.COOLDOWN
    # Hits during cooldown never fire.
    assert all(x.fired is None for x in feed(sm, [(10, H), (20, H), (30, H)]))
    assert sm.state is RuleState.COOLDOWN
    p = sm.poll(now=301)
    assert p.state is RuleState.IDLE and p.previous is RuleState.COOLDOWN and p.changed


def test_truth_persisting_through_cooldown_fires_when_it_ends():
    sm = make(cooldown=300)
    feed(sm, [(0, H), (1, H), (2, M), (3, M)])
    feed(sm, [(100, H), (200, H)])
    r = sm.observe(*H, now=301)
    assert r.fired is not None
    assert r.fired.truth_since == 100
    assert sm.fire_count == 2


def test_rearm_straight_to_idle_when_cooldown_over():
    sm = make(cooldown=5)
    feed(sm, [(0, H), (1, H)])
    r = feed(sm, [(10, M), (11, M)])[-1]
    assert r.state is RuleState.IDLE
    assert feed(sm, [(12, H), (13, H)])[-1].fired is not None


def test_zero_cooldown_refire_after_rearm():
    sm = make(cooldown=0)
    feed(sm, [(0, H), (1, H), (2, M), (3, M)])
    assert sm.state is RuleState.IDLE
    assert feed(sm, [(4, H), (5, H)])[-1].fired is not None
    assert sm.fire_count == 2


def test_rearm_clears_window_fresh_evidence_needed():
    sm = make(k=2, n=3, cooldown=0)
    feed(sm, [(0, H), (1, H), (2, M), (3, M)])
    # A single new hit is not enough even though an old hit might still have been in the
    # window; the episode starts fresh.
    r = sm.observe(*H, now=4)
    assert r.fired is None and r.state is RuleState.CANDIDATE


# --------------------------------------------------------------------------- errors / input


@pytest.mark.parametrize(
    "match, conf",
    [
        (None, 0.9),
        (True, float("nan")),
        (True, float("inf")),
        (True, "0.9"),
        (True, None),
        (True, True),
        (1, 0.9),
        ("yes", 0.9),
    ],
)
def test_invalid_verdicts_are_ignored(match, conf):
    sm = make()
    sm.observe(*H, now=0)
    r = sm.observe(match, conf, now=1)
    assert not r.counted
    assert r.state is RuleState.CANDIDATE
    assert sm.snapshot(now=1)["window"] == 1


def test_errors_between_hits_do_not_break_confirmation():
    sm = make()
    sm.observe(*H, now=0)
    sm.observe(None, 0.0, now=1)
    sm.observe(None, 0.0, now=2)
    assert sm.observe(*H, now=3).fired is not None


def test_confidence_above_one_is_clamped():
    sm = make()
    sm.observe(True, 5.0, now=0)
    r = sm.observe(True, 1.0, now=1)
    assert r.fired.confidence == 1.0


def test_negative_confidence_is_clamped_to_zero_and_misses():
    sm = make()
    r = sm.observe(True, -3.0, now=0)
    assert r.counted and r.state is RuleState.IDLE


def test_integer_confidence_accepted():
    sm = make()
    sm.observe(True, 1, now=0)
    assert sm.observe(True, 1, now=1).fired is not None


# --------------------------------------------------------------------------- k / n variants


def test_k1_n1_fires_on_first_hit_and_rearms_on_one_miss():
    sm = make(k=1, n=1, cooldown=0)
    r = sm.observe(*H, now=0)
    assert r.fired is not None and r.previous is RuleState.IDLE
    assert sm.observe(*M, now=1).state is RuleState.IDLE
    assert sm.observe(*H, now=2).fired is not None


def test_k_equals_n_needs_consecutive_hits():
    sm = make(k=3, n=3)
    results = feed(sm, [(0, H), (1, H), (2, M)])
    assert results[-1].state is RuleState.IDLE  # one miss refutes when k == n
    results = feed(sm, [(3, H), (4, H), (5, H)])
    assert [r.fired is not None for r in results] == [False, False, True]


def test_three_of_five():
    sm = make(k=3, n=5)
    results = feed(sm, [(0, H), (1, M), (2, H), (3, M), (4, H)])
    assert [r.fired is not None for r in results] == [False] * 4 + [True]


def test_three_of_five_refuted_by_three_misses():
    sm = make(k=3, n=5)
    results = feed(sm, [(0, H), (1, M), (2, M), (3, H), (4, M)])
    assert results[-1].state is RuleState.IDLE


def test_window_slides():
    sm = make(k=2, n=3)
    # H M M -> refuted, then M H: candidate with one hit only.
    feed(sm, [(0, H), (1, M), (2, M), (3, M), (4, H)])
    assert sm.state is RuleState.CANDIDATE
    assert sm.observe(*H, now=5).fired is not None


# --------------------------------------------------------------------------- hold


def test_condition_fires_after_hold():
    sm = make("condition", hold=300)
    results = feed(sm, [(0, H), (100, H), (299, H)])
    assert all(r.fired is None for r in results)
    assert sm.state is RuleState.CANDIDATE
    r = sm.observe(*H, now=300)
    assert r.fired is not None
    assert r.fired.held_seconds == 300 and r.fired.truth_since == 0


def test_condition_hold_tolerates_one_miss():
    sm = make("condition", hold=300)
    feed(sm, [(0, H), (100, M), (200, H)])
    assert sm.state is RuleState.CANDIDATE
    r = sm.observe(*H, now=300)
    assert r.fired is not None and r.fired.truth_since == 0


def test_condition_hold_broken_restarts_clock():
    sm = make("condition", hold=300)
    feed(sm, [(0, H), (100, H), (150, M), (200, M)])
    assert sm.state is RuleState.IDLE
    feed(sm, [(250, H), (400, H)])
    assert sm.state is RuleState.CANDIDATE
    r = sm.observe(*H, now=550)
    assert r.fired is not None and r.fired.truth_since == 250


def test_never_fires_on_a_miss_even_if_hold_reached():
    sm = make("condition", hold=300)
    feed(sm, [(0, H), (100, H)])
    r = sm.observe(*M, now=300)
    assert r.fired is None and r.state is RuleState.CANDIDATE
    assert sm.observe(*H, now=301).fired is not None


def test_trigger_ignores_hold_seconds():
    sm = make("trigger", hold=600)
    assert sm.spec.effective_hold_seconds == 0
    assert feed(sm, [(0, H), (1, H)])[-1].fired is not None


def test_condition_with_zero_hold_behaves_like_trigger():
    sm = make("condition", hold=0)
    assert feed(sm, [(0, H), (1, H)])[-1].fired is not None


def test_condition_latches_for_whole_episode():
    sm = make("condition", hold=10, cooldown=0)
    feed(sm, [(0, H), (10, H)])
    assert sm.fire_count == 1
    feed(sm, [(t, H) for t in range(20, 200, 10)])
    assert sm.fire_count == 1


# --------------------------------------------------------------------------- absence


def test_absence_counts_confident_no_as_evidence():
    sm = make("absence", hold=600)
    feed(sm, [(0, M), (300, M)])
    assert sm.state is RuleState.CANDIDATE
    r = sm.observe(*M, now=600)
    assert r.fired is not None and r.fired.mode == "absence"
    assert r.fired.confidence == pytest.approx(0.9)


def test_absence_yes_is_a_miss():
    sm = make("absence", hold=0)
    r = sm.observe(*H, now=0)
    assert r.counted and r.state is RuleState.IDLE


def test_absence_unsure_no_is_a_miss():
    sm = make("absence", hold=0)
    sm.observe(False, 0.4, now=0)
    sm.observe(False, 0.4, now=1)
    assert sm.state is RuleState.IDLE


def test_absence_returning_object_refutes():
    sm = make("absence", hold=600)
    feed(sm, [(0, M), (300, H), (400, H)])
    assert sm.state is RuleState.IDLE


# --------------------------------------------------------------------------- staleness


def test_stale_candidate_is_dropped():
    sm = make(stale=900)
    sm.observe(*H, now=0)
    r = sm.observe(*H, now=1000)
    assert r.fired is None and r.state is RuleState.CANDIDATE
    assert sm.snapshot(now=1000)["truth_seconds"] == 0
    assert sm.observe(*H, now=1001).fired is not None


def test_gap_exactly_at_limit_is_not_stale():
    sm = make(stale=900)
    sm.observe(*H, now=0)
    assert sm.observe(*H, now=900).fired is not None


def test_poll_drops_stale_candidate():
    sm = make(stale=900)
    sm.observe(*H, now=0)
    r = sm.poll(now=901)
    assert r.state is RuleState.IDLE and r.changed
    assert sm.snapshot(now=901)["window"] == 0


def test_stale_firing_rearms_and_can_fire_again():
    sm = make(stale=900, cooldown=300)
    feed(sm, [(0, H), (1, H)])
    r = sm.observe(*H, now=2000)
    assert r.fired is None and r.state is RuleState.CANDIDATE
    assert sm.observe(*H, now=2001).fired is not None
    assert sm.fire_count == 2


def test_stale_firing_within_cooldown_goes_to_cooldown():
    sm = make(stale=10, cooldown=300)
    feed(sm, [(0, H), (1, H)])
    assert sm.poll(now=50).state is RuleState.COOLDOWN
    assert sm.poll(now=301).state is RuleState.IDLE


def test_stale_idle_window_cleared():
    sm = make(stale=10)
    sm.observe(*M, now=0)
    sm.poll(now=100)
    assert sm.snapshot(now=100)["window"] == 0


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_invalid_stale_after(bad):
    with pytest.raises(ValueError):
        make(stale=bad)


# --------------------------------------------------------------------------- clock


def test_injected_clock_used_when_now_omitted():
    t = [0.0]
    sm = make(clock=lambda: t[0])
    sm.observe(*H)
    t[0] = 5.0
    r = sm.observe(*H)
    assert r.fired.ts == 5.0


def test_clock_going_backwards_is_clamped():
    sm = make()
    sm.observe(*H, now=100)
    r = sm.observe(*H, now=50)
    assert r.fired is not None and r.fired.ts == 100 and r.fired.held_seconds == 0


def test_backwards_clock_cannot_shorten_cooldown():
    sm = make(cooldown=300, k=1, n=1)
    sm.observe(*H, now=1000)
    sm.observe(*M, now=1001)
    assert sm.poll(now=0).state is RuleState.COOLDOWN


def test_non_finite_now_raises():
    sm = make()
    with pytest.raises(ValueError):
        sm.observe(*H, now=math.nan)


# --------------------------------------------------------------------------- maintenance


def test_reset_clears_everything():
    sm = make(cooldown=300)
    feed(sm, [(0, H), (1, H), (2, M), (3, M)])
    assert sm.state is RuleState.COOLDOWN
    sm.reset()
    assert sm.state is RuleState.IDLE
    assert feed(sm, [(4, H), (5, H)])[-1].fired is not None


def test_update_spec_threshold_keeps_evidence():
    sm = make(thr=0.7)
    sm.observe(True, 0.75, now=0)
    sm.update_spec(sm.spec.with_changes(confidence_threshold=0.8))
    assert sm.state is RuleState.CANDIDATE
    r = sm.observe(True, 0.75, now=1)  # now below threshold -> miss
    assert r.fired is None
    assert sm.observe(True, 0.85, now=2).fired is not None


def test_update_spec_mode_change_resets_but_keeps_cooldown():
    sm = make(cooldown=300)
    feed(sm, [(0, H), (1, H)])
    sm.update_spec(sm.spec.with_changes(mode="condition", hold_seconds=10))
    assert sm.state is RuleState.COOLDOWN
    assert sm.poll(now=301).state is RuleState.IDLE


def test_update_spec_n_change_resizes_window():
    sm = make(k=2, n=3)
    sm.observe(*H, now=0)
    sm.update_spec(sm.spec.with_changes(confirm_k=1, confirm_n=1))
    assert sm.state is RuleState.IDLE
    assert sm.observe(*H, now=1).fired is not None


def test_snapshot_reports_state():
    sm = make(cooldown=100)
    snap = sm.snapshot(now=0)
    assert snap["state"] == "idle" and snap["fire_count"] == 0
    assert snap["seconds_since_fire"] is None and snap["truth_seconds"] is None
    assert snap["cooldown_remaining"] == 0.0
    feed(sm, [(0, H), (10, H)])
    snap = sm.snapshot(now=40)
    assert snap["state"] == "firing"
    assert snap["hits"] == 2 and snap["misses"] == 0 and snap["window"] == 2
    assert snap["truth_seconds"] == 40
    assert snap["cooldown_remaining"] == 70
    assert snap["seconds_since_fire"] == 30
    assert snap["fire_count"] == 1


def test_snapshot_does_not_advance_clock():
    sm = make()
    sm.snapshot(now=10_000)
    sm.observe(*H, now=0)
    assert sm.observe(*H, now=1).fired is not None


def test_state_values_are_strings():
    assert RuleState.FIRING == "firing"
    assert {s.value for s in RuleState} == {"idle", "candidate", "firing", "cooldown"}


def test_concurrent_observers_fire_once():
    sm = make(cooldown=0)
    fired = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(i):
        start.wait()
        for j in range(200):
            r = sm.observe(*H, now=float(j))
            if r.fired is not None:
                with lock:
                    fired.append(r.fired)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fired) == 1
    assert sm.fire_count == 1


def test_cooling_down_covers_the_latched_firing_state():
    sm = make(cooldown=300)
    feed(sm, [(0, H), (1, H)])
    assert sm.state is RuleState.FIRING
    assert sm.cooling_down(100) and sm.cooling_down(300.5)
    assert not sm.cooling_down(301)
    assert not make().cooling_down(0)
