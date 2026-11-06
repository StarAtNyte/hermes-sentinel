"""Storage and secret shims behave like plugins/plugin_storage.py and agent/secret_scope.py."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from harness import hermes_shims

SECRETS = {"SENTINEL_STREAM_PASSWORD": "hunter2"}


@pytest.fixture
def secrets() -> dict[str, str]:
    return dict(SECRETS)


def test_plugin_data_dir_under_bound_home(shim_binding, plugin_data_root: Path):
    from plugins.plugin_storage import plugin_data_dir

    path = plugin_data_dir("hermes-sentinel")
    assert path == plugin_data_root / "plugin-data" / "hermes-sentinel" and path.is_dir()


def test_plugin_data_dir_fixture(plugin_data_dir: Path, plugin_data_root: Path):
    assert plugin_data_dir.is_relative_to(plugin_data_root) and plugin_data_dir.is_dir()


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".hidden", "x..y", "a" * 65, 3])
def test_plugin_data_dir_rejects_bad_names(shim_binding, name):
    from plugins.plugin_storage import plugin_data_dir

    with pytest.raises(ValueError, match="invalid plugin name"):
        plugin_data_dir(name)


def test_plugin_db_pragmas_and_threads(shim_binding):
    from plugins.plugin_storage import plugin_db

    conn = plugin_db("hermes-sentinel")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.row_factory is None
        conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id))")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO c VALUES (99)")
        result: list[int] = []
        t = threading.Thread(  # check_same_thread=False, like the real helper
            target=lambda: result.append(conn.execute("SELECT 1").fetchone()[0])
        )
        t.start()
        t.join()
        assert result == [1]
    finally:
        conn.close()


@pytest.mark.parametrize("filename", ["", "../x.db", "sub/x.db"])
def test_plugin_db_rejects_bad_filenames(shim_binding, filename):
    from plugins.plugin_storage import plugin_db

    with pytest.raises(ValueError, match="invalid plugin db filename"):
        plugin_db("hermes-sentinel", filename)


def test_plugin_db_validates_plugin_name(shim_binding):
    from plugins.plugin_storage import plugin_db

    with pytest.raises(ValueError):
        plugin_db("../escape")


def test_get_secret_reads_bound_dict_and_records(shim_binding):
    from agent.secret_scope import get_secret, get_secret_str

    assert get_secret("SENTINEL_STREAM_PASSWORD") == "hunter2"
    assert get_secret("MISSING") is None
    assert get_secret("MISSING", "d") == "d"
    assert get_secret_str("MISSING") == ""
    assert shim_binding.secrets_read == [
        "SENTINEL_STREAM_PASSWORD", "MISSING", "MISSING", "MISSING",
    ]
    shim_binding.secrets["NEW"] = "v"
    assert get_secret("NEW") == "v"
    with pytest.raises(ValueError):
        get_secret("")


def test_get_secret_never_reads_process_env(shim_binding, monkeypatch):
    from agent.secret_scope import get_secret

    monkeypatch.setenv("ONLY_IN_ENV", "leak")
    assert get_secret("ONLY_IN_ENV") is None


def test_unbound_storage_raises():
    hermes_shims.install()
    from plugins.plugin_storage import plugin_data_dir

    with pytest.raises(hermes_shims.UnboundShimError):
        plugin_data_dir("hermes-sentinel")


def test_modules_are_stable_across_bindings(tmp_path):
    hermes_shims.install()
    module = sys.modules["agent.secret_scope"]
    get_secret = module.get_secret  # what a plugin binds at import time
    with hermes_shims.bind(home=tmp_path, secrets={"A": "1"}):
        assert get_secret("A") == "1"
    with hermes_shims.bind(home=tmp_path, secrets={"A": "2"}):
        assert sys.modules["agent.secret_scope"] is module
        assert get_secret("A") == "2"
    assert hermes_shims.installed()
    assert sys.modules["agent"].secret_scope is module


def test_uninstall_restores_previous_modules():
    hermes_shims.install()
    saved = {n: sys.modules.get(n) for n in hermes_shims.SHIMMED_MODULES}
    try:
        hermes_shims.uninstall()
        assert not hermes_shims.installed()
        assert all(sys.modules.get(n) is not saved[n] for n in hermes_shims.SHIMMED_MODULES)
    finally:
        hermes_shims.install()
        assert all(sys.modules[n] is saved[n] for n in hermes_shims.SHIMMED_MODULES)
