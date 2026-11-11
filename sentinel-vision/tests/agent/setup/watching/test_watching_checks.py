"""Setup checks about alert delivery, the watcher, caps and rules."""

from __future__ import annotations

from types import SimpleNamespace

from harness.plugin_loader import plugin_module
from harness.setup_rig import FakeAlerter, FakeStore, run

cfg_mod = plugin_module("sentinel.config")


def test_injection_unknown_proven_and_blocked(tmp_path):
    unknown = run(tmp_path)["injection"]
    assert unknown.status == "warn" and "allow_gateway_injection: true" in unknown.fix
    assert "Cannot be verified" in unknown.detail
    assert run(tmp_path, alerter=FakeAlerter(sent=2))["injection"].status == "ok"
    blocked = run(tmp_path, alerter=FakeAlerter(blocked="refused"))["injection"]
    assert blocked.status == "fail" and "allow_gateway_injection: true" in blocked.fix


def test_engine_states(tmp_path):
    assert run(tmp_path, engine=SimpleNamespace(running=True))["engine"].detail == "Running."
    assert (
        "Standing by"
        in run(tmp_path, engine=SimpleNamespace(running=False, standing_by=True))["engine"].detail
    )
    assert "Not started" in run(tmp_path)["engine"].detail


def test_delivery_route(tmp_path):
    assert run(tmp_path, route=None)["delivery"].status == "ok"
    known = run(tmp_path, route={"session_key": "k", "platform": "telegram"})["delivery"]
    assert known.status == "ok"
    unknown = run(tmp_path, route={})["delivery"]
    assert unknown.status == "warn" and "/sentinel here" in unknown.fix


def test_budget_caps(tmp_path):
    def with_caps(**kw):
        return run(tmp_path, config=cfg_mod.SentinelConfig(**kw))["budget"]

    assert with_caps().status == "ok"
    assert with_caps(vision_calls_per_hour=0).status == "fail"
    assert with_caps(vision_calls_per_hour=None).status == "warn"
    assert with_caps(vision_calls_per_hour=100, vision_calls_per_day=50).status == "warn"
    assert with_caps(vision_calls_per_hour=900, vision_calls_per_day=9000).status == "warn"


def test_rules_count_and_offline_source(tmp_path):
    assert "No rules yet" in run(tmp_path)["rules"].detail
    rules = [SimpleNamespace(id="r1", enabled=True), SimpleNamespace(id="r2", enabled=False)]
    engine = SimpleNamespace(
        running=True,
        standing_by=False,
        status=lambda: {"sources": [{"source": "camera:3", "online": False, "rules": ["r1"]}]},
    )
    c = run(tmp_path, store_getter=lambda: FakeStore(tmp_path, rules), engine=engine)["rules"]
    assert c.status == "warn" and "2 rule(s), 1 active" in c.detail
    assert "r1 (camera:3)" in c.detail
    idle = SimpleNamespace(running=False, standing_by=False)
    waiting = run(tmp_path, store_getter=lambda: FakeStore(tmp_path, rules), engine=idle)["rules"]
    assert waiting.status == "warn" and "not running" in waiting.detail
