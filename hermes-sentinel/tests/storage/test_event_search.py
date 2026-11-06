"""Event search (FTS5 and LIKE)."""

from __future__ import annotations

import sqlite3

import pytest
from harness.store_helpers import T0, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- search


def seed(s):
    s.add_event("rdoor", caption="A courier left a package at the door", ts=T0)
    s.add_event(
        "rdesk", caption="Someone picked up the coffee mug", reason="hand visible", ts=T0 + 3600
    )
    s.add_event("rdesk", caption="The mug was placed back on the desk", ts=T0 + 7200)
    s.add_event("rcat", caption="Cat sleeping on the couch 100% relaxed", ts=T0 + 10800)


@pytest.mark.parametrize("which", ["store", "like_store"])
def test_query_text(request, which):
    s = request.getfixturevalue(which)
    assert s.fts_enabled is (which == "store")
    seed(s)
    hits = s.query_events(text="mug")
    assert {e.caption for e in hits} == {
        "Someone picked up the coffee mug",
        "The mug was placed back on the desk",
    }
    assert [e.rule_id for e in s.query_events(text="what happened at the door today?")] == ["rdoor"]
    assert len(s.query_events(text="package OR mug")) == 3
    assert s.query_events(text="hand")[0].rule_id == "rdesk"  # reason is searched
    assert s.query_events(text="giraffe") == []
    assert s.query_events(text="the a of") == []  # only stopwords, too short: nothing
    assert s.query_events(text="?!*") == []


@pytest.mark.parametrize("which", ["store", "like_store"])
def test_query_prefix_and_special_chars(request, which):
    s = request.getfixturevalue(which)
    seed(s)
    assert [e.rule_id for e in s.query_events(text="slee")] == ["rcat"]
    assert len(s.query_events(text='"mug" AND NOT (')) == 2  # FTS syntax is neutralised
    assert s.query_events(text="100%")[0].rule_id == "rcat"
    assert s.query_events(text="_") == []


@pytest.mark.parametrize("which", ["store", "like_store"])
def test_query_filters(request, which):
    s = request.getfixturevalue(which)
    seed(s)
    all_ = s.query_events()
    assert [e.rule_id for e in all_] == ["rcat", "rdesk", "rdesk", "rdoor"]  # newest first
    assert len(s.query_events(rule_id="rdesk")) == 2
    assert len(s.query_events(since=T0 + 3600)) == 3
    assert len(s.query_events(since=T0 + 3600, until=T0 + 7200)) == 1
    assert len(s.query_events(text="mug", until=T0 + 7200)) == 1
    assert len(s.query_events(limit=2)) == 2
    assert len(s.query_events(limit=0)) == 1  # clamped to >= 1
    ev = all_[0]
    s.set_feedback(ev.id, "correct")
    assert [e.id for e in s.query_events(feedback="correct")] == [ev.id]
    assert len(s.query_events(feedback="none")) == 3


def test_fts_rebuilt_after_writes_without_fts(plugin_data_dir, clock):
    s = Store(clock=clock, enable_fts=False)
    s.add_event("r", caption="mug gone")
    s.close()
    s = Store(clock=clock)
    assert s.fts_enabled
    assert len(s.query_events(text="mug")) == 1
    s.close()


def test_fts_query_error_falls_back_to_like(store, monkeypatch):
    seed(store)
    with store.database.tx() as conn:
        conn.execute("DROP TABLE events_fts")
    assert len(store.query_events(text="mug")) == 2


def test_fts_unavailable_detected(plugin_data_dir, clock):
    real = sqlite3.connect

    class NoFts(sqlite3.Connection):
        def execute(self, sql, *a):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *a)

    path = plugin_data_dir / "nofts.db"
    s = Store(clock=clock, connect=lambda n, f: real(path, factory=NoFts, check_same_thread=False))
    assert s.fts_enabled is False
    s.add_event("r", caption="mug")
    assert len(s.query_events(text="mug")) == 1
    s.close()
