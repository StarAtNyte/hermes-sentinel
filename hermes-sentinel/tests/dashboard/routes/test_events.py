"""Event routes: list, filters and feedback."""

from __future__ import annotations

import pytest
from harness.dashboard_rig import (
    BASE,
    add_event,
    watch_rule,
)


def test_events_list_shape_and_order(client, rig):
    rule = watch_rule(rig)
    first = add_event(rig, rule, caption="first", snapshot=False)
    second = add_event(rig, rule, caption="second")
    items = client.get(f"{BASE}/events").json()
    assert [i["id"] for i in items] == [second.id, first.id]
    top = items[0]
    assert set(top) == {
        "id",
        "rule_id",
        "rule_text",
        "ts",
        "caption",
        "confidence",
        "feedback",
        "has_snapshot",
    }
    assert top["has_snapshot"] is True and items[1]["has_snapshot"] is False
    assert top["feedback"] is None and top["rule_text"] and top["confidence"] == 0.91


def test_events_filters_and_limits(client, rig):
    a, b = watch_rule(rig), watch_rule(rig, recipe="desk-intruder")
    for i in range(5):
        add_event(rig, a, caption=f"a{i}", snapshot=False)
    add_event(rig, b, caption="b0", snapshot=False)
    assert len(client.get(f"{BASE}/events?limit=3").json()) == 3
    assert {e["rule_id"] for e in client.get(f"{BASE}/events?rule_id={b}").json()} == {b}
    assert len(client.get(f"{BASE}/events?limit=100000").json()) == 6  # clamped, not an error
    assert client.get(f"{BASE}/events?since=1h").status_code == 200
    assert client.get(f"{BASE}/events?since=2999-01-01T00:00:00Z").json() == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=abc",
        "limit=0",
        "limit=-1",
        "rule_id=bad id!",
        "since=whenever",
    ],
)
def test_events_bad_params_are_400(client, rig, query):
    resp = client.get(f"{BASE}/events?{query}")
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_feedback_correct_and_false_alarm(client, rig):
    rule = watch_rule(rig)
    ev = add_event(rig, rule)
    before = rig.sentinel.store.get_rule(rule).confidence_threshold
    ok = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "correct"}).json()
    assert ok["ok"] is True and ok["event"]["feedback"] == "correct"
    lowered = before - 0.01  # a confirmed alert nudges the threshold down a little
    assert ok["threshold"] == pytest.approx(lowered)
    bad = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "false_alarm"}).json()
    assert bad["event"]["feedback"] == "false_alarm"
    assert bad["threshold"] == pytest.approx(lowered + 0.05)
    assert rig.sentinel.store.get_rule(rule).confidence_threshold == pytest.approx(lowered + 0.05)
    again = client.post(f"{BASE}/events/{ev.id}/feedback", json={"verdict": "false_alarm"}).json()
    assert again["threshold"] == pytest.approx(lowered + 0.05)  # idempotent


@pytest.mark.parametrize("payload", [{}, {"verdict": "maybe"}, {"verdict": 1}, [], "x"])
def test_feedback_bad_body_is_400(client, rig, payload):
    ev = add_event(rig, watch_rule(rig))
    resp = client.post(f"{BASE}/events/{ev.id}/feedback", json=payload)
    assert resp.status_code == 400 and resp.json()["ok"] is False


def test_feedback_malformed_and_oversize_bodies(client, rig):
    ev = add_event(rig, watch_rule(rig))
    url = f"{BASE}/events/{ev.id}/feedback"
    assert client.post(url, content=b"{nope").status_code == 400
    assert client.post(url, json={"verdict": "correct", "pad": "x" * 20000}).status_code == 413


def test_feedback_unknown_event(client, rig):
    resp = client.post(f"{BASE}/events/9999/feedback", json={"verdict": "correct"})
    assert resp.status_code == 404 and resp.json()["ok"] is False
    assert client.post(f"{BASE}/events/x/feedback", json={"verdict": "correct"}).status_code == 400
