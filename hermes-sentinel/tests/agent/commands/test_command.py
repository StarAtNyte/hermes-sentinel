"""``/sentinel`` subcommands: status, list, pause, resume, test, budget, help and here."""

from __future__ import annotations

import pytest
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeSource
from harness.plugin_loader import PLUGIN_NAME
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(
        lambda call: verdict(True, 0.85, "someone at the desk"), task="sentinel_vision"
    )
    return r


def cmd(rig, text=""):
    return rig.ctx.run_command("sentinel", text)


def add_rule(rig):
    return rig.watch(recipe="desk-intruder", source=CAM)["rule"]["rule_id"]


@pytest.fixture
def telegram_ctx(shim_binding):
    ctx = FakeCtx(plugin_id=PLUGIN_NAME, session_surface="telegram")
    yield ctx
    ctx.close()


def test_status_idle_and_watching(rig):
    out = cmd(rig)
    assert out.startswith("hermes-sentinel t: idle; 0 active / 0 rules.")
    assert "Vision checks: 0/60 this hour" in out
    add_rule(rig)
    rig.pump(2)
    out = cmd(rig, "status")
    assert "watching; 1 active / 1 rules" in out
    assert f"source {CAM}: online" in out and "events fired: 0" in out


def test_status_reports_config_problems(fake_ctx):
    rig = Rig(fake_ctx, config={"confirm_k": 9, "confirm_n": 3})
    assert "config: confirm_k (9) is larger than confirm_n (3)" in cmd(rig)


def test_list_pause_resume(rig):
    assert "No watch rules yet" in cmd(rig, "list")
    rule_id = add_rule(rig)
    assert f"{rule_id} [idle] {CAM}" in cmd(rig, "list")
    assert cmd(rig, f"pause {rule_id}") == f"Paused rule {rule_id}."
    assert f"{rule_id} [paused]" in cmd(rig, "ls")
    assert cmd(rig, f"resume {rule_id}") == f"Resumed rule {rule_id}."
    assert "No rule 'zz'" in cmd(rig, "pause zz")
    assert cmd(rig, "pause").startswith("Usage")


def test_test_and_budget(rig):
    rule_id = add_rule(rig)
    out = cmd(rig, f"test {rule_id}")
    assert out.startswith(f"{rule_id}: ") and "-> yes (confidence 0.85" in out
    assert "would count as evidence" in out and "someone at the desk" in out
    assert "Test of zz failed" in cmd(rig, "test zz")
    assert cmd(rig, "test").startswith("Usage")
    assert cmd(rig, "budget").startswith("Vision checks: 1/60 this hour")


def test_help_unknown_and_errors(rig, monkeypatch):
    assert cmd(rig, "help").startswith("Usage")
    assert cmd(rig, "dance").startswith("Unknown subcommand 'dance'")

    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(rig.ctx.commands["sentinel"]["handler"], "budget", boom)
    assert cmd(rig, "budget") == "Sentinel error: kaput"


def test_command_accepts_session_keywords(rig):
    handler = rig.ctx.commands["sentinel"]["handler"]
    assert "idle" in handler("status", session_id="s", session_key="k", platform="cli", source=None)


def test_here_routes_messaging_alerts(telegram_ctx):
    import json
    from types import SimpleNamespace

    fake_ctx = telegram_ctx
    rig = Rig(fake_ctx)
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    handler = fake_ctx.commands["sentinel"]["handler"]
    tool = fake_ctx.tools["sentinel_watch"]["handler"]

    # A rule created from Telegram before the chat identified itself: the agent is told how.
    out = json.loads(tool({"recipe": "pet-on-couch", "source": CAM}, session_id="s-tg"))
    assert "/sentinel here" in out["message"]
    rule_id = out["rule"]["rule_id"]

    src = SimpleNamespace(platform="telegram", chat_id="42", user_id="7")
    assert "Alerts for " + rule_id in handler(
        "here",
        session_id="s-tg",
        session_key="agent:main:telegram:dm:42",
        platform="telegram",
        source=src,
    )
    origin = rig.sentinel.store.get_rule(rule_id).origin
    assert origin["session_key"] == "agent:main:telegram:dm:42" and origin["chat_id"] == "42"

    # New rules from the same session pick the remembered route up automatically.
    out = json.loads(tool({"recipe": "desk-intruder", "source": CAM}, session_id="s-tg"))
    assert "/sentinel here" not in out["message"]
    origin = rig.sentinel.store.get_rule(out["rule"]["rule_id"]).origin
    assert origin["session_key"] == "agent:main:telegram:dm:42"

    # Delivery uses the chat origin (platform + chat id), never a bare session key.
    rig.sentinel.alerter.notice("hello", origin=origin)
    assert fake_ctx.injected[-1].session_key is None
    assert fake_ctx.injected[-1].origin["chat_id"] == "42"


def test_here_variants(rig):
    rule_id = add_rule(rig)
    handler = rig.ctx.commands["sentinel"]["handler"]
    assert "did not identify itself" in handler("here")
    assert "No rule 'zz'" in handler("here zz", session_key="k")
    assert f"Alerts for {rule_id}" in handler("here", session_id="other", session_key="k")
    assert "from now on" in handler("here", session_id="other", session_key="k")
    assert handler(f"here {rule_id}", session_key="k2").endswith("will now come to this chat.")
    assert rig.sentinel.store.get_rule(rule_id).origin["session_key"] == "k2"
    assert rule_id in handler("here all", session_key="k3")
    assert rig.engine.state.rules[rule_id].spec.origin["session_key"] == "k3"
