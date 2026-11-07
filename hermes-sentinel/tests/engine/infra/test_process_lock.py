"""The exclusive engine lock file."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
judge_mod = plugin_module("sentinel.vision")


def test_process_lock_is_exclusive(tmp_path):
    a = engine_mod.ProcessLock(tmp_path / "x" / "engine.lock")
    b = engine_mod.ProcessLock(tmp_path / "x" / "engine.lock")
    assert a.acquire() and a.held and a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
    b.release()
