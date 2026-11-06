"""Snapshots and events."""

from __future__ import annotations

from datetime import datetime

import pytest
from harness.store_helpers import JPEG, T0, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

# --------------------------------------------------------------------------- snapshots


def test_save_snapshot_layout(store, plugin_data_dir):
    p = store.save_snapshot(JPEG, ts=T0 + 3.5, rule_id="r/../evil")
    assert p.parent == (plugin_data_dir / "snapshots" / "2025-11-06").resolve()
    assert p.name.startswith("120003500_revil_") and p.suffix == ".jpg"
    assert p.read_bytes() == JPEG
    assert not [f for f in p.parent.iterdir() if f.suffix == ".tmp"]
    p2 = store.save_snapshot(JPEG, ts=T0 + 3.5, rule_id="r/../evil")
    assert p2 != p


def test_save_snapshot_uses_clock(store, clock):
    clock.t = T0 + 86400
    assert store.save_snapshot(JPEG).parent.name == "2025-11-07"


@pytest.mark.parametrize(
    "data, exc",
    [
        (b"PNG", ValueError),
        ("str", TypeError),
        (b"\xff\xd8\xff" + b"0" * (20 * 1024 * 1024), ValueError),
    ],
)
def test_save_snapshot_rejects(store, data, exc):
    with pytest.raises(exc):
        store.save_snapshot(data)


def test_save_snapshot_accepts_bytearray(store):
    assert store.save_snapshot(bytearray(JPEG)).exists()


# --------------------------------------------------------------------------- events


def test_add_and_get_event(store):
    snap = store.save_snapshot(JPEG)
    ev = store.add_event(
        "rmug",
        caption="mug taken",
        reason="hand",
        confidence=0.92,
        source="webcam:0",
        snapshot_path=snap,
        ts=T0,
    )
    assert ev.id > 0 and ev.ts == "2025-11-06T12:00:00.000Z"
    assert ev.snapshot_path == str(snap) and ev.feedback is None
    assert ev.confidence == pytest.approx(0.92)
    assert store.get_event(ev.id) == ev
    assert store.get_event(9999) is None
    d = ev.to_dict()
    assert d["caption"] == "mug taken" and d["source"] == "webcam:0"
    # relative paths are stored
    assert store.database.read("SELECT snapshot_path FROM events")[0][0].startswith("snapshots/")


def test_add_event_relative_snapshot_path(store):
    snap = store.save_snapshot(JPEG)
    rel = snap.relative_to(store.data_dir)
    ev = store.add_event("r", snapshot_path=str(rel))
    assert ev.snapshot_path == str(snap)


def test_add_event_rejects_outside_paths(store, tmp_path):
    with pytest.raises(StoreError, match="outside"):
        store.add_event("r", snapshot_path=tmp_path / "x.jpg")
    with pytest.raises(StoreError):
        store.add_event("r", snapshot_path="../../etc/passwd")
    with pytest.raises(ValueError):
        store.add_event("")


@pytest.mark.parametrize(
    "conf, expected", [(1.5, 1.0), (-1, 0.0), (float("nan"), None), (None, None)]
)
def test_event_confidence_sanitised(store, conf, expected):
    assert store.add_event("r", confidence=conf).confidence == expected


def test_event_text_clipped(store):
    ev = store.add_event("r", caption="c" * 5000, reason="r" * 5000)
    assert len(ev.caption) == store_mod.MAX_CAPTION_LEN
    assert len(ev.reason) == store_mod.MAX_REASON_LEN


def test_event_ts_variants(store, clock):
    assert store.add_event("r").ts == store_mod.to_iso(T0)
    assert store.add_event("r", ts="2025-11-06T13:00:00+01:00").ts == "2025-11-06T12:00:00.000Z"
    assert store.add_event("r", ts=datetime(2025, 1, 1)).ts == "2025-01-01T00:00:00.000Z"


def test_feedback(store):
    ev = store.add_event("r", caption="x")
    assert store.set_feedback(ev.id, "false_alarm").feedback == "false_alarm"
    assert store.set_feedback(ev.id, None).feedback is None
    assert store.set_feedback(424242, "correct") is None
    with pytest.raises(ValueError):
        store.set_feedback(ev.id, "meh")


def test_last_event_and_count(store):
    assert store.last_event("r") is None
    store.add_event("r", ts=T0)
    late = store.add_event("r", ts=T0 + 10)
    store.add_event("r", ts=T0 + 5)
    store.add_event("other", ts=T0 + 20)
    assert store.last_event("r").id == late.id
    assert store.count_events() == 4
    assert store.count_events(rule_id="r") == 3
    assert store.count_events(since=T0 + 5) == 3
