"""Reconnect backoff and offline notices."""

from __future__ import annotations

from harness.fake_sources import SourceError
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")
sources_mod = plugin_module("sentinel.sources")


def test_source_backoff_and_single_offline_notice(h):
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("cable pulled")
    src.open_error = SourceError("device busy")
    attempts = []
    for _ in range(40):
        if h.engine.step_source("cam") == "offline":
            attempts.append(h.clock.now())
        h.clock.advance(0.5)
    gaps = [round(b - a, 1) for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps[:4] == [1.0, 2.0, 4.0, 8.0] and set(gaps[4:]) <= {8.0}
    assert len(h.notices("offline")) == 1
    assert "device busy" in h.notices("offline")[0]
    status = h.engine.status()["sources"][0]
    assert status["online"] is False and status["error"] == "device busy"
    src.open_error = src.read_error = None
    for _ in range(20):
        h.engine.step_source("cam")
        h.clock.advance(0.5)
    assert len(h.notices("back online")) == 1
    assert h.engine.status()["sources"][0]["online"] is True


def test_short_blip_inside_grace_sends_no_notice(h):
    h.add()
    src = h.sources["cam"]
    src.read_error = sources_mod.SourceDisconnected("blip")
    h.engine.step_source("cam")
    src.read_error = None
    h.clock.advance(1.0)
    assert h.engine.step_source("cam") == "frame"
    assert h.notices() == []


def test_not_ready_is_not_a_failure(h):
    h.add()
    h.sources["cam"].read_error = sources_mod.SourceNotReady("empty folder")
    assert h.engine.step_source("cam") == "not_ready"
    assert h.engine.status()["sources"][0]["error"] == ""


def test_unknown_source_is_offline_not_fatal(h):
    spec = rules.RuleSpec(question="q?", source="ghost", id="g").validate()
    h.store.upsert_rule(spec)
    h.engine.add_rule(spec)
    assert h.engine.step_source("ghost") == "offline"
    assert "unknown ghost" in h.engine.rule_status("g")["source_error"]
