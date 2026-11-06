"""Schema, migrations and opening/closing the database."""

from __future__ import annotations

import sqlite3

import pytest
from harness.store_helpers import rule, store_mod

Store = store_mod.Store
StoreError = store_mod.StoreError

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
    rows = s2._database.read("SELECT COUNT(*) FROM schema_migrations")
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


@pytest.mark.parametrize(
    "field, value",
    [("retention_days", 0), ("max_storage_mb", -1), ("retention_days", float("nan"))],
)
def test_invalid_limits(store, field, value):
    with pytest.raises(ValueError):
        setattr(store, field, value)


def test_custom_data_dir_and_connect(tmp_path, clock):
    db = tmp_path / "x.db"
    s = Store(data_dir=tmp_path / "data", connect=lambda n, f: sqlite3.connect(db), clock=clock)
    s.upsert_rule(rule())
    assert s.data_dir == (tmp_path / "data").resolve()
    s.close()
