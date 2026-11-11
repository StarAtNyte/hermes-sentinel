"""HeldAlerts: counting, clearing, persistence and the status line."""

from __future__ import annotations

import json

from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")


def test_hold_counts_and_keeps_the_latest_event():
    book = alerts.HeldAlerts()
    book.hold("r1", 3)
    held = book.hold("r1", 5)
    assert held == alerts.HeldAlert(event_id=5, count=2)
    assert book.get("r1") == held and book.get("other") is None


def test_clear_forgets_only_that_rule():
    book = alerts.HeldAlerts()
    book.hold("r1", 1)
    book.hold("r2", 2)
    book.clear("r1")
    book.clear("never-held")
    assert book.get("r1") is None and book.get("r2") is not None


def test_held_alerts_survive_a_restart(tmp_path):
    path = tmp_path / "held.json"
    alerts.HeldAlerts(path).hold("r1", 9)
    assert alerts.HeldAlerts(path).get("r1") == alerts.HeldAlert(event_id=9, count=1)


def test_unreadable_or_malformed_files_are_ignored(tmp_path):
    path = tmp_path / "held.json"
    path.write_text("{not json", encoding="utf-8")
    assert alerts.HeldAlerts(path).get("r1") is None
    path.write_text(json.dumps({"r1": {"event_id": "x"}, "r2": 5, "r3": {"event_id": 4}}))
    book = alerts.HeldAlerts(path)
    assert book.get("r1") is None and book.get("r2") is None
    assert book.get("r3") == alerts.HeldAlert(event_id=4, count=1)


def test_description_names_the_resume_command():
    text = alerts.describe_held("r1", alerts.HeldAlert(event_id=7, count=3))
    assert text.startswith("alert held for rule r1")
    assert "3 held" in text and "/sentinel resume r1 here" in text


def test_rebound_origin_takes_the_caller_session_unless_anonymous():
    old = {"session_id": "old", "session_key": "k-old"}
    caller = {"session_id": "new", "surface": "cli"}
    assert alerts.rebound_origin(old, caller) == caller
    assert alerts.rebound_origin(old, {"surface": "cli"}) == old
