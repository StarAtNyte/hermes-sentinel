"""Concurrent writers and transaction rollback."""

from __future__ import annotations

import threading

import pytest
from harness.store_helpers import store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- concurrency


def test_concurrent_event_writes(store):
    errors = []

    def worker(i):
        try:
            for j in range(25):
                store.add_event(f"r{i}", caption=f"mug event {j}")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.count_events() == 150
    assert len(store.query_events(text="mug", limit=500)) == 150


def test_failed_transaction_rolls_back(store):
    with pytest.raises(RuntimeError), store._database.tx() as conn:
        conn.execute("INSERT INTO events(rule_id, ts) VALUES ('r', 'x')")
        raise RuntimeError("boom")
    assert store.count_events() == 0
    store.add_event("r")  # connection still usable
    assert store.count_events() == 1
