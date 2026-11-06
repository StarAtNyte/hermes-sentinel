"""Rule persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness.store_helpers import JPEG, rule, rules, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- rules


def test_rule_crud(store):
    a = store.upsert_rule(rule("ra"))
    store.upsert_rule(rule("rb", created_at="2000-01-01T00:00:00Z"))
    assert store.get_rule("ra") == a
    assert store.get_rule("nope") is None
    assert [r.id for r in store.list_rules()] == ["rb", "ra"]  # by created_at
    paused = store.set_rule_enabled("ra", False)
    assert paused.enabled is False
    assert [r.id for r in store.list_rules(enabled_only=True)] == ["rb"]
    assert store.set_rule_enabled("nope", True) is None
    store.upsert_rule(store.get_rule("rb").with_changes(question="Changed?"))
    assert store.get_rule("rb").question == "Changed?"
    assert store.delete_rule("ra") is True
    assert store.delete_rule("ra") is False


def test_upsert_validates(store):
    bad = rule()
    bad.confirm_k = 99
    with pytest.raises(rules.RuleValidationError):
        store.upsert_rule(bad)
    assert store.list_rules() == []


def test_corrupt_rule_row_skipped(store):
    store.upsert_rule(rule("rgood"))
    with store.database.tx() as conn:
        conn.execute("INSERT INTO rules VALUES ('rbad', '{not json', 1, 'a', 'a')")
    assert [r.id for r in store.list_rules()] == ["rgood"]
    assert store.get_rule("rbad") is None


def test_delete_rule_removes_examples_keeps_events(store):
    store.upsert_rule(rule())
    ex = store.add_example("rmug", "pos", JPEG)
    snap = store.save_snapshot(JPEG, rule_id="rmug")
    ev = store.add_event("rmug", caption="mug taken", snapshot_path=snap)
    assert store.delete_rule("rmug")
    assert not Path(ex.path).exists()
    assert store.list_examples("rmug") == []
    assert store.get_event(ev.id) is not None and snap.exists()


def test_delete_rule_purge_events(store):
    store.upsert_rule(rule())
    snap = store.save_snapshot(JPEG, rule_id="rmug")
    ev = store.add_event("rmug", caption="mug taken", snapshot_path=snap)
    store.add_event("rother", caption="mug elsewhere")
    store.delete_rule("rmug", purge_events=True)
    assert store.get_event(ev.id) is None and not snap.exists()
    assert [e.rule_id for e in store.query_events(text="mug")] == ["rother"]
