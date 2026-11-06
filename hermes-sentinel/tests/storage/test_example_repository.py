"""Teaching examples."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness.store_helpers import JPEG, rule, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- examples


def test_examples_from_bytes_event_and_path(store):
    store.upsert_rule(rule())
    a = store.add_example("rmug", "pos", JPEG, note="mug present")
    assert Path(a.path).read_bytes() == JPEG and a.note == "mug present"
    assert "/examples/rmug/" in a.path
    snap = store.save_snapshot(JPEG)
    ev = store.add_event("rmug", snapshot_path=snap)
    b = store.add_example("rmug", "neg", event_id=ev.id)
    assert b.event_id == ev.id and Path(b.path) != snap and Path(b.path).exists()
    c = store.add_example("rmug", "neg", path=snap)
    assert [e.id for e in store.list_examples("rmug")] == [c.id, b.id, a.id]
    assert [e.id for e in store.list_examples("rmug", "neg", limit=1)] == [c.id]
    assert store.list_examples("rmug", "pos")[0].to_dict()["kind"] == "pos"


def test_example_errors(store, tmp_path):
    store.upsert_rule(rule())
    with pytest.raises(ValueError):
        store.add_example("rmug", "maybe", JPEG)
    with pytest.raises(StoreError, match="unknown rule"):
        store.add_example("rnope", "pos", JPEG)
    with pytest.raises(StoreError, match="unknown event"):
        store.add_example("rmug", "pos", event_id=12345)
    ev = store.add_event("rmug")  # no snapshot
    with pytest.raises(ValueError):
        store.add_example("rmug", "pos", event_id=ev.id)
    with pytest.raises(ValueError):
        store.add_example("rmug", "pos")
    with pytest.raises(StoreError, match="outside"):
        store.add_example("rmug", "pos", path=tmp_path / "x.jpg")
    with pytest.raises(StoreError):
        store.add_example("rmug", "pos", path="snapshots/missing.jpg")
    with pytest.raises(ValueError):
        store.add_example("rmug", "pos", b"notjpeg")
    assert store.list_examples("rmug") == []


def test_examples_capped_per_kind(plugin_data_dir, clock):
    s = Store(clock=clock, max_examples_per_kind=2)
    s.upsert_rule(rule())
    made = [s.add_example("rmug", "pos", JPEG) for _ in range(4)]
    neg = s.add_example("rmug", "neg", JPEG)
    kept = s.list_examples("rmug", "pos")
    assert [e.id for e in kept] == [made[3].id, made[2].id]
    assert not Path(made[0].path).exists() and not Path(made[1].path).exists()
    assert s.list_examples("rmug", "neg") == [neg]
    s.close()


def test_delete_example(store):
    store.upsert_rule(rule())
    ex = store.add_example("rmug", "pos", JPEG)
    assert store.delete_example(ex.id) and not Path(ex.path).exists()
    assert store.delete_example(ex.id) is False
