"""``sentinel_events``."""

from __future__ import annotations

from datetime import datetime, timedelta

from harness.tool_calls import watch


def seed_events(rig, rule_id):
    store = rig.sentinel.store
    now = datetime.now().astimezone()
    old = store.add_event(rule_id, caption="package at the door", ts=now - timedelta(days=2))
    new = store.add_event(rule_id, caption="cat on the couch", confidence=0.8)
    return old, new


def test_events_query_text_time_feedback(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    old, new = seed_events(rig, rule_id)
    assert rig.call("sentinel_events")["count"] == 2
    out = rig.call("sentinel_events", since="today")
    assert [e["event_id"] for e in out["events"]] == [new.id]
    assert out["events"][0]["rule"]
    assert rig.call("sentinel_events", text="package")["events"][0]["event_id"] == old.id
    assert rig.call("sentinel_events", since="3d", until="1d")["count"] == 1
    assert rig.call("sentinel_events", feedback="false_alarm")["count"] == 0
    assert rig.call("sentinel_events", limit=1)["count"] == 1
    out = rig.call("sentinel_events", text="nothing-like-this")
    assert out["count"] == 0 and out["summary"] == "No matching events."
    assert "cannot understand time" in rig.call("sentinel_events", since="soonish")["error"]
    assert rig.call("sentinel_events", feedback="meh")["ok"] is False
