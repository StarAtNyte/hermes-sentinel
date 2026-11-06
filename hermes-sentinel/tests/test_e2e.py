"""End-to-end: tools -> engine -> gate -> judge -> state machine -> store -> alert, on FakeCtx.

Every scenario runs the real modules (only the camera, the clock and the model are fake):

* a watch rule on ``object_removed`` fires exactly one alert, with a MEDIA photo;
* "false alarm" feedback stores a negative example and tightens the rule;
* ``sentinel_events`` finds the event;
* budget exhaustion pauses judging and posts exactly one notice;
* a disconnect gives exactly one offline and one back-online notice;
* nuisance scenes (flicker, shake, noise, ...) produce zero alerts and no extra vision calls;
* the threaded engine does the same with real threads and stops cleanly.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from harness.fake_sources import FakeSource, SourceError
from harness.scenes import NUISANCE_SCENARIOS, build_scene
from sentinel_rig import HoldLastSource, Rig, verdict

CAM = "camera:7"


def mug_removed_rig(fake_ctx, **kw):
    scene = build_scene("object_removed", n_frames=40, remove_at=10)
    rig = Rig(fake_ctx, **kw)
    src = HoldLastSource(scene, rig.clock)
    rig.sources[CAM] = src
    remove_at = scene.meta["event_frame"]

    def judge(call):
        index = src.position - 1  # the frame the engine just read
        gone = index >= remove_at
        return verdict(gone, 0.92, "a hand picked up the red mug" if gone else "mug on desk")

    fake_ctx.llm.respond(judge, task="sentinel_vision")
    fake_ctx.llm.script(
        {"question": "Is the red mug gone from the desk?", "mode": "trigger"},
        purpose="sentinel:compile",
    )
    return rig, src


def test_object_removed_fires_exactly_one_alert_with_media(fake_ctx):
    rig, _src = mug_removed_rig(fake_ctx)
    out = rig.watch("tell me if someone takes my coffee mug", source=CAM)
    assert out["rule"]["question"] == "Is the red mug gone from the desk?"
    assert out["rule"]["compiled_by"] == "llm"
    assert "Watching" in out["message"] and "60 vision checks/hour" in out["message"]
    rig.pump(60)

    alerts = rig.alerts()
    assert len(alerts) == 1, alerts
    text = alerts[0]
    assert "a hand picked up the red mug" in text and "confidence 0.92" in text
    media = [line for line in text.splitlines() if line.startswith("MEDIA:")]
    assert len(media) == 1
    path = Path(media[0][len("MEDIA:"):])
    assert path.is_file() and path.read_bytes()[:3] == b"\xff\xd8\xff"
    assert "sentinel_feedback" in text and "Event #1" in text
    # Gate before judge: the first-frame tick, the onset and one confirmation, nothing else.
    vision = fake_ctx.llm.calls_for(task="sentinel_vision")
    assert 2 <= len(vision) <= 4
    assert all(c.purpose == f"sentinel:{out['rule']['rule_id']}" for c in vision)
    assert all(len(c.images) >= 1 for c in vision)


def test_false_alarm_feedback_adds_negative_example_and_tightens(fake_ctx):
    rig, _ = mug_removed_rig(fake_ctx)
    rule_id = rig.watch("tell me if someone takes my coffee mug", source=CAM)["rule"]["rule_id"]
    rig.pump(60)
    assert len(rig.alerts()) == 1

    out = rig.call("sentinel_feedback", event_id=1, verdict="false_alarm", note="it was me")
    assert out["ok"], out
    assert out["confidence_threshold"] == pytest.approx(0.75)
    store = rig.sentinel.store
    negatives = store.list_examples(rule_id, "neg")
    assert len(negatives) == 1 and Path(negatives[0].path).is_file()
    assert store.get_event(1).feedback == "false_alarm"
    assert store.get_rule(rule_id).confidence_threshold == pytest.approx(0.75)
    assert rig.engine.rule_status(rule_id) is not None

    # The next vision call carries the negative example as a labelled reference image.
    fake_ctx.llm.calls.clear()
    out = rig.call("sentinel_update", rule_id=rule_id, action="edit",
                   changes={"question": "Is the red mug gone?"})
    assert out["ok"]
    result = rig.engine.test_rule(rule_id)
    assert result["ok"]
    call = fake_ctx.llm.calls_for(task="sentinel_vision")[-1]
    texts = [b.text for b in call.blocks if b.type == "text"]
    assert any("answer is NO" in t for t in texts)
    assert len(call.images) == 2


def test_events_query_returns_the_event(fake_ctx):
    rig, _ = mug_removed_rig(fake_ctx)
    rig.watch("tell me if someone takes my coffee mug", source=CAM)
    rig.pump(60)
    out = rig.call("sentinel_events", text="mug", since="today")
    assert out["ok"] and out["count"] == 1
    event = out["events"][0]
    assert event["event_id"] == 1 and "mug" in event["caption"]
    assert event["media"].startswith("MEDIA:")
    assert "mug" in out["summary"]
    assert rig.call("sentinel_events", text="giraffe")["count"] == 0


def test_budget_exhaustion_pauses_judging_with_one_notice(fake_ctx):
    rig = Rig(fake_ctx, config={"vision_calls_per_hour": 2, "idle_check_seconds": 1})
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=30), rig.clock,
                                  loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9), task="sentinel_vision")
    rig.watch(recipe="print-failure", source=CAM)  # condition rule: idle ticks apply
    rig.pump(120)  # 12 virtual seconds: ~12 idle ticks, only 2 may reach the model
    assert len(fake_ctx.llm.calls_for(task="sentinel_vision")) == 2
    notices = rig.notices("budget")
    assert len(notices) == 1, rig.notices()
    assert "Vision checks: 2/2 this hour" in notices[0]
    assert rig.alerts() == []
    status = rig.call("sentinel_rules")["rules"][0]
    assert status["vision_checks"]["this_hour"] == 2


def test_disconnect_gives_one_offline_and_one_online_notice(fake_ctx):
    rig = Rig(fake_ctx)
    src = FakeSource(build_scene("disconnect", n_frames=12), rig.clock)
    rig.sources[CAM] = src
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9), task="sentinel_vision")
    rig.watch(recipe="pet-on-couch", source=CAM)
    rig.pump(12)
    assert rig.notices() == []

    src.open_error = SourceError("camera unplugged")  # reopening fails for a while
    for _ in range(20):
        rig.pump(1)
        rig.clock.advance(1.0)
    offline = rig.notices("offline")
    assert len(offline) == 1, rig.notices()
    assert "camera unplugged" in offline[0]
    status = rig.call("sentinel_rules")["rules"][0]["status"]
    assert status.startswith("source offline")

    src.open_error = None
    for _ in range(10):  # wait out the backoff, reconnect, read a few frames
        rig.pump(1)
        rig.clock.advance(1.0)
    assert len(rig.notices("back online")) == 1
    assert len(rig.notices("offline")) == 1
    assert len(rig.notices()) == 2
    assert src.open_count >= 2


@pytest.mark.parametrize("name", NUISANCE_SCENARIOS)
def test_nuisance_scenes_produce_zero_alerts(fake_ctx, name):
    rig = Rig(fake_ctx)
    scene = build_scene(name)
    rig.sources[CAM] = FakeSource(scene, rig.clock)
    calls = {"n": 0}

    def gullible(call):
        # Says "no" to the start-up tick, then "yes" to anything: any frame the gate lets
        # through would end in an alert.
        calls["n"] += 1
        return verdict(calls["n"] > 1, 0.95, "something happened")

    fake_ctx.llm.respond(gullible, task="sentinel_vision")
    rig.watch(recipe="desk-intruder", source=CAM)
    rig.pump(len(scene))
    assert rig.alerts() == []
    assert len(fake_ctx.llm.calls_for(task="sentinel_vision")) == 1


def test_threaded_engine_alerts_and_stops_cleanly(fake_ctx):
    scene = build_scene("object_removed", n_frames=40, remove_at=10)
    rig = Rig(fake_ctx, threaded=True, use_process_lock=True)
    src = HoldLastSource(scene, rig.clock)
    rig.sources[CAM] = src
    remove_at = scene.meta["event_frame"]
    lock = threading.Lock()

    def judge(call):
        with lock:
            gone = src.position - 1 >= remove_at
        return verdict(gone, 0.9, "the mug is gone" if gone else "mug on desk")

    fake_ctx.llm.respond(judge, task="sentinel_vision")
    rig.watch(recipe="desk-intruder", source=CAM)
    engine = rig.engine
    assert engine.running
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not rig.alerts():
        time.sleep(0.02)
    assert len(rig.alerts()) == 1
    assert (rig.sentinel.store.data_dir / "engine.lock").exists()
    fake_ctx.unload()  # ctx.on_unload -> Sentinel.shutdown -> Engine.stop
    assert not engine.running
    alive = [t.name for t in threading.enumerate() if t.name.startswith("sentinel-")]
    assert alive == []
    assert src.close_count >= 1
