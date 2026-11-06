"""Store: schema/migrations, rules, events + search (FTS5 and LIKE), examples, usage, pruning."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness.plugin_loader import plugin_module

store_mod = plugin_module("sentinel.store")
rules = plugin_module("sentinel.rules")
Store = store_mod.Store
StoreError = store_mod.StoreError

T0 = 1_762_430_400.0  # 2025-11-06T12:00:00Z
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 1000


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(plugin_data_dir, clock):
    s = Store(clock=clock)
    yield s
    s.close()


@pytest.fixture
def like_store(plugin_data_dir, clock):
    s = Store(clock=clock, enable_fts=False, db_filename="like.db")
    yield s
    s.close()


def rule(rid="rmug", **kw):
    return rules.RuleSpec(question="Is the mug there?", source="cam", id=rid, **kw).validate()


# --------------------------------------------------------------------------- helpers


def test_to_iso_fixed_width_and_parse():
    assert store_mod.to_iso(T0) == "2025-11-06T12:00:00.000Z"
    assert store_mod.to_iso(T0 + 0.1234) == "2025-11-06T12:00:00.123Z"
    naive = datetime(2025, 1, 2, 3, 4, 5)
    assert store_mod.to_iso(naive) == "2025-01-02T03:04:05.000Z"
    assert store_mod.parse_ts("2025-01-02T03:04:05Z") == naive.replace(tzinfo=UTC)
    assert store_mod.parse_ts("2025-01-02T05:04:05+02:00") == naive.replace(tzinfo=UTC)
    assert store_mod.parse_ts("2025-01-02T03:04:05") == naive.replace(tzinfo=UTC)
    assert store_mod.hour_bucket(T0 + 59 * 60) == "2025-11-06T12"
    assert store_mod.to_iso().endswith("Z")
    with pytest.raises(TypeError):
        store_mod.to_iso("now")
    with pytest.raises(ValueError):
        store_mod.to_iso(float("nan"))


def test_day_range():
    since, until = store_mod.day_range(datetime(2025, 11, 6, 15, 30))
    assert (since, until) == ("2025-11-06T00:00:00.000Z", "2025-11-07T00:00:00.000Z")


# --------------------------------------------------------------------------- schema


def test_opens_with_schema_and_fts(store, plugin_data_dir):
    assert store.schema_version() == store_mod.SCHEMA_VERSION == 1
    assert store.fts_enabled
    assert store.data_dir == plugin_data_dir.resolve()
    assert (plugin_data_dir / "sentinel.db").exists()


def test_reopen_is_idempotent(plugin_data_dir, clock):
    s1 = Store(clock=clock)
    s1.upsert_rule(rule())
    s1.close()
    s2 = Store(clock=clock)
    assert s2.schema_version() == 1
    assert [r.id for r in s2.list_rules()] == ["rmug"]
    rows = s2._read("SELECT COUNT(*) FROM schema_migrations")
    assert rows[0][0] == 1
    s2.close()


def test_newer_schema_refused(plugin_data_dir, clock):
    Store(clock=clock).close()
    conn = sqlite3.connect(plugin_data_dir / "sentinel.db")
    conn.execute("INSERT INTO schema_migrations VALUES (99, 'x')")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        Store(clock=clock)


def test_open_failure_wrapped(plugin_data_dir):
    def boom(name, filename):
        raise sqlite3.OperationalError("disk gone")

    with pytest.raises(StoreError, match="disk gone"):
        Store(connect=boom)


def test_closed_store_raises(store):
    store.close()
    store.close()  # idempotent
    with pytest.raises(StoreError, match="closed"):
        store.list_rules()


def test_context_manager(plugin_data_dir, clock):
    with Store(clock=clock) as s:
        s.upsert_rule(rule())
    with pytest.raises(StoreError):
        s.get_rule("rmug")


@pytest.mark.parametrize("field, value", [("retention_days", 0), ("max_storage_mb", -1),
                                          ("retention_days", float("nan"))])
def test_invalid_limits(store, field, value):
    with pytest.raises(ValueError):
        setattr(store, field, value)


def test_custom_data_dir_and_connect(tmp_path, clock):
    db = tmp_path / "x.db"
    s = Store(data_dir=tmp_path / "data", connect=lambda n, f: sqlite3.connect(db), clock=clock)
    s.upsert_rule(rule())
    assert s.data_dir == (tmp_path / "data").resolve()
    s.close()


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
    with store._tx() as conn:
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


@pytest.mark.parametrize("data, exc", [(b"PNG", ValueError), ("str", TypeError),
                                       (b"\xff\xd8\xff" + b"0" * (20 * 1024 * 1024), ValueError)])
def test_save_snapshot_rejects(store, data, exc):
    with pytest.raises(exc):
        store.save_snapshot(data)


def test_save_snapshot_accepts_bytearray(store):
    assert store.save_snapshot(bytearray(JPEG)).exists()


# --------------------------------------------------------------------------- events


def test_add_and_get_event(store):
    snap = store.save_snapshot(JPEG)
    ev = store.add_event("rmug", caption="mug taken", reason="hand", confidence=0.92,
                         source="webcam:0", snapshot_path=snap, ts=T0)
    assert ev.id > 0 and ev.ts == "2025-11-06T12:00:00.000Z"
    assert ev.snapshot_path == str(snap) and ev.feedback is None
    assert ev.confidence == pytest.approx(0.92)
    assert store.get_event(ev.id) == ev
    assert store.get_event(9999) is None
    d = ev.to_dict()
    assert d["caption"] == "mug taken" and d["source"] == "webcam:0"
    # relative paths are stored
    assert store._read("SELECT snapshot_path FROM events")[0][0].startswith("snapshots/")


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


@pytest.mark.parametrize("conf, expected", [(1.5, 1.0), (-1, 0.0), (float("nan"), None),
                                            (None, None)])
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


# --------------------------------------------------------------------------- search


def seed(s):
    s.add_event("rdoor", caption="A courier left a package at the door", ts=T0)
    s.add_event("rdesk", caption="Someone picked up the coffee mug", reason="hand visible",
                ts=T0 + 3600)
    s.add_event("rdesk", caption="The mug was placed back on the desk", ts=T0 + 7200)
    s.add_event("rcat", caption="Cat sleeping on the couch 100% relaxed", ts=T0 + 10800)


@pytest.mark.parametrize("which", ["store", "like_store"])
def test_query_text(request, which):
    s = request.getfixturevalue(which)
    assert s.fts_enabled is (which == "store")
    seed(s)
    hits = s.query_events(text="mug")
    assert {e.caption for e in hits} == {"Someone picked up the coffee mug",
                                         "The mug was placed back on the desk"}
    assert [e.rule_id for e in s.query_events(text="what happened at the door today?")] == [
        "rdoor"]
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
    with store._tx() as conn:
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
    s = Store(clock=clock, connect=lambda n, f: real(path, factory=NoFts,
                                                    check_same_thread=False))
    assert s.fts_enabled is False
    s.add_event("r", caption="mug")
    assert len(s.query_events(text="mug")) == 1
    s.close()


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


# --------------------------------------------------------------------------- usage


def test_usage_caps(store, clock):
    for _ in range(3):
        assert store.try_consume_usage(hour_cap=3, day_cap=10, rule_id="ra").allowed
    r = store.try_consume_usage(hour_cap=3, day_cap=10, rule_id="rb")
    assert (r.allowed, r.hour_used, r.day_used) == (False, 3, 3)
    clock.t += 3600
    r = store.try_consume_usage(2, hour_cap=3, day_cap=10, rule_id="rb")
    assert (r.allowed, r.hour_used, r.day_used) == (True, 2, 5)
    assert store.usage_totals() == (2, 5)
    assert store.usage_totals(rule_id="ra") == (0, 3)
    assert store.usage_totals(rule_id="rb") == (2, 2)


def test_usage_day_cap_rolls_hourly(store, clock):
    assert store.try_consume_usage(5, hour_cap=None, day_cap=5).allowed
    clock.t += 23 * 3600
    assert not store.try_consume_usage(hour_cap=None, day_cap=5).allowed
    clock.t += 3600  # first bucket now outside the 24 h window
    assert store.try_consume_usage(hour_cap=None, day_cap=5).allowed
    assert store.usage_totals() == (1, 1)


def test_usage_unlimited_zero_and_negative(store):
    assert store.try_consume_usage(1000, hour_cap=None, day_cap=None).allowed
    assert not store.try_consume_usage(hour_cap=0, day_cap=None).allowed
    assert not store.try_consume_usage(0, hour_cap=0, day_cap=None).allowed  # already over
    with pytest.raises(ValueError):
        store.try_consume_usage(-1, hour_cap=None, day_cap=None)


def test_usage_zero_calls_fit_an_unused_zero_cap(store):
    r = store.try_consume_usage(0, hour_cap=0, day_cap=0)
    assert r.allowed and store.usage_totals() == (0, 0)


def test_usage_persists(plugin_data_dir, clock):
    s = Store(clock=clock)
    s.try_consume_usage(4, hour_cap=None, day_cap=None)
    s.close()
    s = Store(clock=clock)
    assert s.usage_totals() == (4, 4)
    s.close()


def test_usage_concurrent_never_exceeds_cap(store):
    allowed = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            ok = store.try_consume_usage(hour_cap=50, day_cap=None).allowed
            with lock:
                allowed.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 50
    assert store.usage_totals()[0] == 50


def test_usage_concurrent_two_connections(plugin_data_dir, clock):
    a, b = Store(clock=clock), Store(clock=clock)
    results = []

    def worker(s):
        for _ in range(15):
            results.append(s.try_consume_usage(hour_cap=20, day_cap=None).allowed)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 20
    a.close()
    b.close()


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
    with pytest.raises(RuntimeError), store._tx() as conn:
        conn.execute("INSERT INTO events(rule_id, ts) VALUES ('r', 'x')")
        raise RuntimeError("boom")
    assert store.count_events() == 0
    store.add_event("r")  # connection still usable
    assert store.count_events() == 1


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
    assert all(Path(r.snapshot_path).exists() for r in remaining[len(pruned):])
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
    assert store._unlink("../../keep.jpg") == 0
    assert store._unlink(str(outside)) == 0
    assert outside.exists()
