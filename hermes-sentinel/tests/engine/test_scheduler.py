"""Periodic maintenance."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision")


def test_maintenance_prunes_and_polls(h, monkeypatch):
    h.add()
    calls = []
    monkeypatch.setattr(h.store, "prune_if_due", lambda: calls.append(1))
    h.engine.maintenance()
    assert calls == [1]
