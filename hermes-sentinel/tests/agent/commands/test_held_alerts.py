"""A refused alert is held, shown by status/list, and delivered state is cleared on resume."""

from __future__ import annotations

import json

import pytest
from harness.fake_sources import FakeSource
from harness.scenes import build_scene
from sentinel_rig import Rig

CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    rig = Rig(fake_ctx)
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    return rig


def handler(rig):
    return rig.ctx.commands["sentinel"]["handler"]


def watch(rig, **kwargs):
    args = {"recipe": "desk-intruder", "source": CAM}
    out = json.loads(rig.ctx.tools["sentinel_watch"]["handler"](args, **kwargs))
    assert out["ok"], out
    return out["rule"]["rule_id"]


def fire(rig, rule_id):
    """Fire one alert for the rule through the real alerter and store."""
    spec = rig.sentinel.store.get_rule(rule_id)
    event = rig.sentinel.store.add_event(rule_id, caption="hand", reason="r", confidence=0.9)
    return rig.sentinel.alerter.alert_event(spec, event, source_name=CAM)


def refuse_injection(rig):
    rig.ctx.classic_cli = False
    rig.ctx.gateway_live = False


def test_refused_alert_is_held_and_shown_by_status_and_list(rig):
    rule_id = watch(rig, session_id="dead-session", session_key="dead-key")
    refuse_injection(rig)
    assert not fire(rig, rule_id).ok
    note = f"alert held for rule {rule_id}: run /sentinel resume {rule_id} here to receive it"
    assert note in handler(rig)("status", session_id="s")
    assert note in handler(rig)("list", session_id="s")


def test_resume_rebinds_to_the_calling_session_and_clears_the_note(rig):
    rule_id = watch(rig, session_id="dead-session", session_key="dead-key")
    refuse_injection(rig)
    fire(rig, rule_id)
    out = handler(rig)(f"resume {rule_id}", session_id="live-session", session_key="live-key")
    assert "its alerts now come to this session" in out and "held" in out
    origin = rig.sentinel.store.get_rule(rule_id).origin
    assert origin["session_id"] == "live-session" and origin["session_key"] == "live-key"
    assert "alert held" not in handler(rig)("status", session_id="live-session")


def test_delivered_alert_clears_an_older_held_note(rig):
    rule_id = watch(rig, session_id="s", session_key="k")
    refuse_injection(rig)
    fire(rig, rule_id)
    rig.ctx.gateway_live = True
    rig.clock.advance(60.0)
    assert fire(rig, rule_id).ok
    assert rig.sentinel.held_alerts.get(rule_id) is None


def test_resume_without_a_held_alert_just_rebinds(rig):
    rule_id = watch(rig, session_id="old")
    out = handler(rig)(f"resume {rule_id}", session_id="new")
    assert out == f"Resumed rule {rule_id}; its alerts now come to this session."
