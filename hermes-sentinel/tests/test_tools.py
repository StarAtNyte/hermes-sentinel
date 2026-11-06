"""The eight agent tools: schemas, argument handling, friendly errors, and each behaviour."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest
import yaml
from harness.fake_sources import FakeSource
from harness.plugin_loader import PLUGIN_DIR, plugin_module
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

tools = plugin_module("sentinel.tools")
sources_mod = plugin_module("sentinel.sources")
CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty desk"), task="sentinel_vision")
    return r


def watch(rig, **args):
    args.setdefault("recipe", "desk-intruder")
    args.setdefault("source", CAM)
    return rig.call("sentinel_watch", **args)


# --------------------------------------------------------------------------- schemas


def test_schemas_are_well_formed_and_match_manifest():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert sorted(manifest["provides_tools"]) == sorted(tools.TOOL_NAMES)
    assert len(tools.TOOL_NAMES) == 8
    import jsonschema

    for name, schema in tools.SCHEMAS.items():
        assert schema["name"] == name and len(schema["description"]) > 20
        params = schema["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        jsonschema.Draft202012Validator.check_schema(params)
        for prop, spec in params["properties"].items():
            assert "type" in spec or "enum" in spec, (name, prop)
        for req in params.get("required", []):
            assert req in params["properties"]


def test_registered_with_toolset_and_emoji(rig):
    for name in tools.TOOL_NAMES:
        entry = rig.ctx.tools[name]
        assert entry["toolset"] == "sentinel" and entry["emoji"] and entry["description"]


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_every_handler_returns_json_even_for_garbage(rig, name):
    handler = rig.ctx.tools[name]["handler"]
    for args in ({}, {"rule_id": 5, "event_id": "x", "kind": [], "source": {}}, None):
        out = json.loads(handler(args, task_id="t", session_id="s"))
        assert isinstance(out, dict) and "ok" in out
    out = json.loads(handler("not a dict"))
    assert out == {"ok": False, "error": "arguments must be a JSON object"}


def test_unexpected_exceptions_are_reported_briefly(rig, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("secret internals")

    monkeypatch.setattr(rig.sentinel.store, "list_rules", boom)
    out = rig.call("sentinel_rules")
    assert out["ok"] is False and "ZeroDivisionError" in out["error"]
    assert "secret internals" not in out["error"]


# --------------------------------------------------------------------------- sources


def test_sentinel_sources(rig, monkeypatch):
    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return [sources_mod.SourceInfo(spec="camera:0", kind="camera", name="Webcam 0",
                                       available=True)]

    monkeypatch.setattr(tools, "list_sources", fake_list)
    out = rig.call("sentinel_sources", probe_cameras=False)
    assert out["ok"] and out["sources"][0]["spec"] == "camera:0"
    assert seen["probe_cameras"] is False and out["default_source"] == "camera:0"


# --------------------------------------------------------------------------- watch


def test_watch_with_recipe(rig):
    out = watch(rig)
    assert out["ok"], out
    rule = out["rule"]
    assert rule["recipe"] == "desk-intruder" and rule["source"] == CAM
    assert rule["compiled_by"] == "manual"
    assert "Checks only when something moves" in out["message"]
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
        {"recipe": "pet-on-couch", "source": CAM}, task_id="t1", session_id="sess-1")
    rule_id = json.loads(raw)["rule"]["rule_id"]
    origin = rig.sentinel.store.get_rule(rule_id).origin
    assert origin["session_id"] == "sess-1" and origin["surface"] == "cli"


def test_watch_compiles_plain_english_with_overrides(rig):
    rig.ctx.llm.script({"question": "Is the stove on?", "mode": "trigger"},
                       purpose="sentinel:compile")
    out = rig.call("sentinel_watch", what="tell me if the stove is left on", source=CAM,
                   roi=[0.5, 0.5, 0.5, 0.5], hold_seconds=120, cooldown_seconds=60,
                   action_prompt="text my partner", schedule={"start": "22:00", "end": "06:00"})
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
        ({"recipe": "pet-on-couch", "source": CAM, "schedule": {"start": "25:00"}},
         "invalid schedule"),
        ({"what": "x" * 5000, "source": CAM}, "too long"),
    ],
)
def test_watch_friendly_errors(rig, args, needle):
    out = rig.call("sentinel_watch", **args)
    assert out["ok"] is False and needle in out["error"], out


def test_watch_rule_cap(rig, monkeypatch):
    monkeypatch.setattr(tools, "MAX_RULES", 1)
    assert watch(rig)["ok"]
    out = watch(rig)
    assert out["ok"] is False and "too many rules" in out["error"]


# --------------------------------------------------------------------------- rules / update


def test_rules_lists_status(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    rig.pump(3)
    out = rig.call("sentinel_rules")
    assert out["ok"] and out["count"] == 1
    entry = out["rules"][0]
    assert entry["rule_id"] == rule_id and entry["status"] == "idle"
    assert entry["vision_checks"]["this_hour"] == 1  # the start-up tick
    assert entry["checks_since_load"] == 1 and entry["last_event"] is None
    assert "Vision checks" in out["budget"]
    assert rig.call("sentinel_rules", rule_id="zzz")["ok"] is False


def test_update_pause_resume_edit_delete(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    out = rig.call("sentinel_update", rule_id=rule_id, action="pause")
    assert out["ok"] and out["rule"]["enabled"] is False
    assert rig.call("sentinel_rules")["rules"][0]["status"] == "paused"
    assert rig.engine.source_keys() == []
    out = rig.call("sentinel_update", rule_id=rule_id, action="resume")
    assert out["rule"]["enabled"] is True and rig.engine.source_keys() == [CAM]
    out = rig.call("sentinel_update", rule_id=rule_id, action="edit", changes={
        "question": "Is anyone at the desk?", "mode": "condition", "hold_seconds": 30,
        "confidence_threshold": 0.8, "roi": None, "action_prompt": "say hi",
        "schedule": None, "cooldown_seconds": 10})
    assert out["ok"], out
    rule = out["rule"]
    assert rule["question"] == "Is anyone at the desk?" and rule["mode"] == "condition"
    assert rule["action"] == "agent" and rule["confidence_threshold"] == 0.8
    out = rig.call("sentinel_update", rule_id=rule_id, action="edit",
                   changes={"action_prompt": ""})
    assert out["rule"]["action"] == "notify"
    out = rig.call("sentinel_update", rule_id=rule_id, action="delete")
    assert out["ok"] and "events are kept" in out["message"]
    assert rig.sentinel.store.get_rule(rule_id) is None and rig.engine.rule_ids() == []


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"action": "pause"}, "'rule_id' is required"),
        ({"rule_id": "nope", "action": "pause"}, "no rule with id"),
        ({"action": "explode"}, "action must be"),
        ({"action": "edit"}, "non-empty 'changes'"),
        ({"action": "edit", "changes": {"enabled": False}}, "cannot change"),
        ({"action": "edit", "changes": {"mode": "sometimes"}}, "mode must be"),
        ({"action": "edit", "changes": {"confidence_threshold": 3}}, "invalid rule"),
        ({"action": "edit", "changes": {"source": "nonsense:::"}}, "invalid source"),
    ],
)
def test_update_errors(rig, args, needle):
    rule_id = watch(rig)["rule"]["rule_id"]
    args.setdefault("rule_id", rule_id)
    if args.get("rule_id") is None or "'rule_id' is required" in needle:
        args.pop("rule_id")
    out = rig.call("sentinel_update", **args)
    assert out["ok"] is False and needle in out["error"], out


# --------------------------------------------------------------------------- look


def test_look_describes_and_saves_photo(rig):
    rig.ctx.llm.script({"answer": "An empty desk.", "caption": "empty desk"},
                       purpose="sentinel:look")
    out = rig.call("sentinel_look", source=CAM, question="Is anyone there?")
    assert out["ok"], out
    assert out["answer"] == "An empty desk." and out["media"].startswith("MEDIA:")
    assert rig.sentinel.store.usage_totals(rule_id="look")[0] == 1


def test_look_errors(rig):
    rig.ctx.llm.script(RuntimeError("no vision"), purpose="sentinel:look")
    out = rig.call("sentinel_look", source=CAM)
    assert out["ok"] is False and "could not answer" in out["error"] and out["media"]
    out = rig.call("sentinel_look", source="camera:9")
    assert out["ok"] is False and "could not read the source" in out["error"]


def test_look_respects_budget(fake_ctx):
    rig = Rig(fake_ctx, config={"vision_calls_per_hour": 0})
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    out = rig.call("sentinel_look", source=CAM)
    assert out["ok"] is False and "Vision checks: 0/0" in out["error"]
    assert len(fake_ctx.llm.calls) == 0


# --------------------------------------------------------------------------- events


def seed_events(rig, rule_id):
    store = rig.sentinel.store
    now = datetime.now().astimezone()
    old = store.add_event(rule_id, caption="package at the door", ts=now - timedelta(days=2))
    new = store.add_event(rule_id, caption="cat on the couch", confidence=0.8)
    return old, new


def test_events_query_text_time_feedback(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    old, new = seed_events(rig, rule_id)
    assert rig.call("sentinel_events")["count"] == 2
    out = rig.call("sentinel_events", since="today")
    assert [e["event_id"] for e in out["events"]] == [new.id]
    assert out["events"][0]["rule"]
    assert rig.call("sentinel_events", text="package")["events"][0]["event_id"] == old.id
    assert rig.call("sentinel_events", since="3d", until="1d")["count"] == 1
    assert rig.call("sentinel_events", feedback="false_alarm")["count"] == 0
    assert rig.call("sentinel_events", limit=1)["count"] == 1
    out = rig.call("sentinel_events", text="nothing-like-this")
    assert out["count"] == 0 and out["summary"] == "No matching events."
    assert "cannot understand time" in rig.call("sentinel_events", since="soonish")["error"]
    assert rig.call("sentinel_events", feedback="meh")["ok"] is False


def test_parse_time_bound():
    now = datetime(2026, 11, 6, 15, 30).astimezone()
    p = tools.parse_time_bound
    assert p(None, now) is None and p("", now) is None
    assert p("today", now) == now.replace(hour=0, minute=0)
    assert p("yesterday", now) == now.replace(hour=0, minute=0) - timedelta(days=1)
    assert p("2h", now) == now - timedelta(hours=2)
    assert p("30 min ago", now) == now - timedelta(minutes=30)
    assert p("1 week", now) == now - timedelta(days=7)
    assert p("now", now) == now and p("last hour", now) == now - timedelta(hours=1)
    assert p("2026-11-01T10:00:00Z", now).utcoffset() == timedelta(0)
    assert p("2026-11-01T10:00:00", now).tzinfo is not None
    with pytest.raises(tools.ToolError):
        p(5, now)
    with pytest.raises(tools.ToolError):
        p("whenever", now)


# --------------------------------------------------------------------------- feedback


def fire_event(rig, rule_id):
    jpeg = cv2.imencode(".jpg", np.zeros((20, 20, 3), np.uint8))[1].tobytes()
    path = rig.sentinel.store.save_snapshot(jpeg, rule_id=rule_id)
    return rig.sentinel.store.add_event(rule_id, caption="x", snapshot_path=path)


def test_feedback_correct_adds_positive_example(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    ev = fire_event(rig, rule_id)
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    assert out["ok"] and out["example_id"] and "'yes' example" in out["message"]
    assert len(rig.sentinel.store.list_examples(rule_id, "pos")) == 1


def test_false_alarm_threshold_is_capped(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    for _ in range(8):
        ev = fire_event(rig, rule_id)
        out = rig.call("sentinel_feedback", event_id=f"#{ev.id}", verdict="false alarm")
        assert out["ok"], out
    assert out["confidence_threshold"] == 0.95
    assert rig.engine._rules[rule_id].spec.confidence_threshold == 0.95


def test_feedback_errors_and_deleted_rule(rig):
    rule_id = watch(rig)["rule"]["rule_id"]
    assert "no event #99" in rig.call("sentinel_feedback", event_id=99,
                                      verdict="correct")["error"]
    assert rig.call("sentinel_feedback", event_id=1, verdict="meh")["ok"] is False
    assert rig.call("sentinel_feedback", event_id=1.5, verdict="correct")["ok"] is False
    ev = rig.sentinel.store.add_event(rule_id, caption="no photo")
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="false_alarm")
    assert out["ok"] and "example_id" not in out
    rig.call("sentinel_update", rule_id=rule_id, action="delete")
    out = rig.call("sentinel_feedback", event_id=ev.id, verdict="correct")
    assert out["ok"] and "no longer exists" in out["message"]


# --------------------------------------------------------------------------- teach


def test_teach_from_files_event_and_source(rig, tmp_path):
    rule_id = watch(rig)["rule"]["rule_id"]
    img = np.full((30, 40, 3), 90, np.uint8)
    jpg, png = tmp_path / "a.jpg", tmp_path / "b.png"
    cv2.imwrite(str(jpg), img)
    cv2.imwrite(str(png), img)
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", image_path=str(jpg))
    assert out["ok"] and out["examples"] == {"yes": 1, "no": 0}
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="no", image_path=str(png), note="me")
    assert out["ok"] and out["examples"] == {"yes": 1, "no": 1}
    ev = fire_event(rig, rule_id)
    assert rig.call("sentinel_teach", rule_id=rule_id, kind="no", event_id=ev.id)["ok"]
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", from_source=True)
    assert out["ok"] and out["examples"] == {"yes": 2, "no": 2}


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({}, "exactly one"),
        ({"image_path": "/x.jpg", "event_id": 1}, "exactly one"),
        ({"image_path": "/nope/missing.jpg"}, "no image file"),
        ({"image_path": "/etc/passwd"}, "must be a .jpg"),
        ({"event_id": 4242}, "no event #4242"),
        ({"kind": "maybe", "event_id": 1}, "kind must be"),
    ],
)
def test_teach_errors(rig, extra, needle):
    rule_id = watch(rig)["rule"]["rule_id"]
    args = {"rule_id": rule_id, "kind": "yes", **extra}
    out = rig.call("sentinel_teach", **args)
    assert out["ok"] is False and needle in out["error"], out


def test_teach_rejects_non_images(rig, tmp_path):
    rule_id = watch(rig)["rule"]["rule_id"]
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"not an image at all")
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", image_path=str(fake))
    assert out["ok"] is False and "not an image" in out["error"]
