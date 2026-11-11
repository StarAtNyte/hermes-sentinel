"""``sentinel_watch``: recipes, compiling, origins, limits and friendly errors."""

from __future__ import annotations

import json

import pytest
from harness.plugin_loader import plugin_module
from harness.tool_calls import CAM, watch


def test_watch_with_recipe(rig):
    out = watch(rig)
    assert out["ok"], out
    rule = out["rule"]
    assert rule["recipe"] == "desk-intruder" and rule["source"] == CAM
    assert rule["compiled_by"] == "manual"
    assert "periodic checks may still occur" in out["message"]
    assert "allow_gateway_injection: true" in out["message"]
    assert "'/sentinel here'" in out["message"]
    assert rig.engine.running and rig.engine.rule_ids() == [rule["rule_id"]]
    assert rig.sentinel.store.get_rule(rule["rule_id"]).origin == {"surface": "cli"}


def test_watch_asks_for_sentinel_here_once_per_session(rig):
    handler = rig.ctx.tools["sentinel_watch"]["handler"]
    first = json.loads(handler({"recipe": "pet-on-couch", "source": CAM}, session_id="s1"))
    second = json.loads(handler({"recipe": "desk-intruder", "source": CAM}, session_id="s1"))
    other = json.loads(handler({"recipe": "pet-on-couch", "source": CAM}, session_id="s2"))
    assert "'/sentinel here'" in first["message"] and "'/sentinel here'" in other["message"]
    assert "'/sentinel here'" not in second["message"]
    assert "allow_gateway_injection: true" in second["message"]


def test_watch_records_origin_from_kwargs(rig):
    raw = rig.ctx.tools["sentinel_watch"]["handler"](
        {"recipe": "pet-on-couch", "source": CAM}, task_id="t1", session_id="sess-1"
    )
    rule_id = json.loads(raw)["rule"]["rule_id"]
    origin = rig.sentinel.store.get_rule(rule_id).origin
    assert origin["session_id"] == "sess-1" and origin["surface"] == "cli"


def test_watch_compiles_plain_english_with_overrides(rig):
    rig.ctx.llm.script(
        {"question": "Is the stove on?", "mode": "trigger"}, purpose="sentinel:compile"
    )
    out = rig.call(
        "sentinel_watch",
        what="tell me if the stove is left on",
        source=CAM,
        roi=[0.5, 0.5, 0.5, 0.5],
        hold_seconds=120,
        cooldown_seconds=60,
        action_prompt="text my partner",
        schedule={"start": "22:00", "end": "06:00"},
    )
    assert out["ok"], out
    rule = out["rule"]
    assert rule["question"] == "Is the stove on?" and rule["mode"] == "condition"
    assert rule["hold_seconds"] == 120 and rule["cooldown_seconds"] == 60
    assert rule["roi"] == [0.5, 0.5, 0.5, 0.5] and rule["action"] == "agent"
    assert rule["action_prompt"] == "text my partner"
    assert rule["schedule"]["start"] == "22:00"
    assert "once it has held for 120s" in out["message"]


def test_watch_falls_back_offline(rig):
    rig.ctx.llm.script(RuntimeError("offline"), purpose="sentinel:compile")
    out = rig.call("sentinel_watch", what="tell me when a package arrives", source=CAM)
    assert out["ok"] and out["rule"]["compiled_by"] == "fallback"


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"source": CAM}, "say what to watch for"),
        ({"recipe": "nope", "source": CAM}, "unknown recipe"),
        ({"recipe": "pet-on-couch", "source": "bogus:thing:x"}, "invalid source"),
        ({"recipe": "pet-on-couch", "source": CAM, "roi": [0.9, 0.9, 0.5, 0.5]}, "roi"),
        ({"recipe": "pet-on-couch", "source": CAM, "roi": [1, 2]}, "roi"),
        ({"recipe": "pet-on-couch", "source": CAM, "hold_seconds": -1}, "non-negative"),
        (
            {"recipe": "pet-on-couch", "source": CAM, "schedule": {"start": "25:00"}},
            "invalid schedule",
        ),
        ({"what": "x" * 5000, "source": CAM}, "too long"),
    ],
)
def test_watch_friendly_errors(rig, args, needle):
    out = rig.call("sentinel_watch", **args)
    assert out["ok"] is False and needle in out["error"], out


def test_watch_rule_cap(rig, monkeypatch):
    monkeypatch.setattr(plugin_module("sentinel.agent.tools.management.watch"), "MAX_RULES", 1)
    assert watch(rig)["ok"]
    out = watch(rig)
    assert out["ok"] is False and "too many rules" in out["error"]
