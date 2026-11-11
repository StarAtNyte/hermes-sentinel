"""Pruning and storage accounting."""

from __future__ import annotations

import os
from pathlib import Path

from harness.store_helpers import JPEG, T0, rule, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- pruning


def age(path: Path, seconds: float, now: float = T0) -> None:
    os.utime(path, (now - seconds, now - seconds))


def test_prune_retention(store, clock):
    old_snap = store.save_snapshot(JPEG, ts=T0 - 8 * 86400)
    old = store.add_event("r", caption="old mug", snapshot_path=old_snap, ts=T0 - 8 * 86400)
    new_snap = store.save_snapshot(JPEG)
    new = store.add_event("r", caption="new mug", snapshot_path=new_snap)
    store.try_consume_usage(hour_cap=None, day_cap=None, now=T0 - 40 * 86400)
    store.try_consume_usage(hour_cap=None, day_cap=None)
    report = store.prune()
    assert report.events_deleted == 1 and report.snapshots_deleted == 1
    assert report.bytes_freed == len(JPEG) and report.usage_rows_deleted == 1
    assert store.get_event(old.id) is None and not old_snap.exists()
    assert not old_snap.parent.exists()  # empty day folder removed
    assert store.get_event(new.id) is not None and new_snap.exists()
    assert [e.id for e in store.query_events(text="mug")] == [new.id]  # FTS cleaned
    assert report.bytes_after == len(JPEG)
    assert report.to_dict()["events_deleted"] == 1


def test_prune_storage_cap_drops_oldest_snapshots(plugin_data_dir, clock):
    big = b"\xff\xd8\xff" + b"0" * (400 * 1024)  # ~0.4 MB
    s = Store(clock=clock, max_storage_mb=1.0)
    s.upsert_rule(rule())
    s.add_example("rmug", "pos", big)  # counts toward the cap but is never pruned
    events = []
    for i in range(4):
        snap = s.save_snapshot(big, ts=T0 + i)
        events.append(s.add_event("rmug", caption=f"e{i}", snapshot_path=snap, ts=T0 + i))
    report = s.prune()
    assert report.events_deleted == 0
    assert report.bytes_after <= 1024 * 1024
    remaining = [s.get_event(e.id) for e in events]
    assert all(r is not None for r in remaining)  # history kept
    pruned = [r for r in remaining if r.snapshot_path is None]
    assert pruned == remaining[: len(pruned)] and 1 <= len(pruned) < 4  # oldest first
    assert all(Path(r.snapshot_path).exists() for r in remaining[len(pruned) :])
    assert len(s.list_examples("rmug")) == 1
    s.close()


def test_prune_orphans_and_tmp(store, clock):
    orphan_old = store.save_snapshot(JPEG)
    age(orphan_old, 7200)
    orphan_new = store.save_snapshot(JPEG)
    tmp = orphan_new.parent / ".stray.abc.tmp"
    tmp.write_bytes(b"x")
    report = store.prune()
    assert report.orphans_deleted == 2
    assert not orphan_old.exists() and not tmp.exists()
    assert orphan_new.exists()  # may be about to get its event row


def test_prune_ignores_non_dirs_and_missing_root(store, plugin_data_dir):
    assert store.prune().events_deleted == 0  # no snapshots dir yet
    (plugin_data_dir / "snapshots").mkdir()
    (plugin_data_dir / "snapshots" / "README").write_text("x")
    store.prune()


def test_prune_missing_snapshot_file_tolerated(store):
    snap = store.save_snapshot(JPEG, ts=T0 - 9 * 86400)
    store.add_event("r", snapshot_path=snap, ts=T0 - 9 * 86400)
    snap.unlink()
    report = store.prune()
    assert report.events_deleted == 1 and report.snapshots_deleted == 0


def test_prune_if_due(store, clock):
    assert store.prune_if_due() is not None
    clock.t += 1800
    assert store.prune_if_due() is None
    clock.t += 1800
    assert store.prune_if_due() is not None
    assert store.prune_if_due(interval=0) is not None


def test_storage_bytes(store):
    assert store.storage_bytes() == 0
    store.save_snapshot(JPEG)
    store.upsert_rule(rule())
    store.add_example("rmug", "neg", JPEG)
    assert store.storage_bytes() == 2 * len(JPEG)


def test_unlink_never_leaves_data_dir(store, tmp_path):
    outside = tmp_path / "keep.jpg"
    outside.write_bytes(JPEG)
    assert store._images.unlink("../../keep.jpg") == 0
    assert store._images.unlink(str(outside)) == 0
    assert outside.exists()
