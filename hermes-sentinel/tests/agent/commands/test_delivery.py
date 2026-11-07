"""``/sentinel here`` auto-binding, the delivery self-test and chat-session matching."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeSource
from harness.plugin_loader import PLUGIN_NAME
from harness.scenes import build_scene
from sentinel_rig import Rig

CAM = "camera:7"
TG_KEY = "agent:main:telegram:dm:42"
TG = {"platform": "telegram", "source": SimpleNamespace(platform="telegram", chat_id="42")}


def make_rig(ctx):
    rig = Rig(ctx)
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    return rig


@pytest.fixture
def rig(fake_ctx):
    return make_rig(fake_ctx)


@pytest.fixture
def telegram_rig(shim_binding):
    ctx = FakeCtx(plugin_id=PLUGIN_NAME, session_surface="telegram")
    yield make_rig(ctx)
    ctx.close()


def handler(rig):
    return rig.ctx.commands["sentinel"]["handler"]


def watch(rig, recipe="desk-intruder", **kwargs):
    out = json.loads(
        rig.ctx.tools["sentinel_watch"]["handler"]({"recipe": recipe, "source": CAM}, **kwargs)
    )
    assert out["ok"], out
    return out["rule"]["rule_id"]


# --------------------------------------------------------------------------- here


def test_here_binds_the_only_unrouted_rule(rig):
    rule_id = watch(rig, session_id="old")
    out = handler(rig)("here", session_id="new", session_key="k-new")
    assert out == f"Alerts for {rule_id} will now come to this chat."
    assert rig.sentinel.store.get_rule(rule_id).origin["session_key"] == "k-new"


def test_here_lists_commands_when_several_rules_lack_a_route(rig):
    first = watch(rig, session_id="a")
    second = watch(rig, recipe="pet-on-couch", session_id="b")
    out = handler(rig)("here", session_id="new", session_key="k-new")
    assert f"/sentinel here {first}" in out and f"/sentinel here {second}" in out
    assert "session_key" not in rig.sentinel.store.get_rule(first).origin
    assert handler(rig)(f"here {first}", session_id="new", session_key="k-new").startswith(
        f"Alerts for {first}"
    )


def test_here_in_a_chat_never_touches_another_chats_rules(telegram_rig):
    watch(telegram_rig, session_id="other-chat-session")
    out = handler(telegram_rig)("here", session_id="mine", session_key=TG_KEY, **TG)
    assert "from now on" in out


# --------------------------------------------------------------------------- self-test


def test_setup_without_flag_sends_nothing(rig):
    out = handler(rig)("setup", session_key="k")
    assert not rig.ctx.injected and "Delivery test" not in out
    assert "--test-delivery" in out


def test_setup_test_delivery_reports_delivered(rig):
    out = handler(rig)("setup --test-delivery", session_key="k")
    assert len(rig.ctx.injected) == 1
    assert "Delivery test: delivered" in out
    assert "[ok]   Alert delivery permission" in out


def test_setup_test_delivery_blocked_shows_yaml_and_sticks(rig):
    rig.ctx.gateway_injection_allowed = False
    rig.ctx.classic_cli = False
    out = handler(rig)("setup --test-delivery", session_key="k")
    assert "Delivery test: BLOCKED" in out and "allow_gateway_injection: true" in out
    assert rig.sentinel.alerter.delivery_blocked()
    assert "Alert delivery is blocked" in handler(rig)("status", session_key="k")
    rig.ctx.gateway_injection_allowed = True
    rig.clock.advance(61.0)
    assert "delivered" in handler(rig)("setup --test-delivery", session_key="k")
    assert rig.sentinel.alerter.delivery_blocked() is None


def test_delivery_test_is_throttled_to_one_a_minute(rig):
    first = rig.call("sentinel_setup_check", test_delivery=True)
    assert first["delivery_test"]["delivered"] is True
    again = rig.call("sentinel_setup_check", test_delivery=True)
    assert again["delivery_test"]["delivered"] is False
    assert "already tested recently" in again["report"]
    assert len(rig.ctx.injected) == 1
    rig.clock.advance(61.0)
    assert rig.call("sentinel_setup_check", test_delivery=True)["delivery_test"]["delivered"]
    assert len(rig.ctx.injected) == 2


def test_setup_check_tool_test_delivery_is_opt_in(rig):
    plain = rig.call("sentinel_setup_check")
    assert "delivery_test" not in plain and not rig.ctx.injected
    tested = rig.call("sentinel_setup_check", test_delivery=True)
    assert tested["delivery_test"]["delivered"] is True
    assert len(rig.ctx.injected) == 1 and "Delivery test: delivered" in tested["report"]


# --------------------------------------------------------------------------- chat sessions


def test_any_subcommand_adopts_this_sessions_rules(telegram_rig):
    rule_id = watch(telegram_rig, session_id="s-tg")
    assert "session_key" not in telegram_rig.sentinel.store.get_rule(rule_id).origin
    handler(telegram_rig)("status", session_id="s-tg", session_key=TG_KEY, **TG)
    origin = telegram_rig.sentinel.store.get_rule(rule_id).origin
    assert origin["session_key"] == TG_KEY and origin["chat_id"] == "42"


def test_new_session_of_the_same_chat_sees_the_rule_without_a_hint(telegram_rig):
    rule_id = watch(telegram_rig, session_id="s-old")
    handler(telegram_rig)("status", session_id="s-old", session_key=TG_KEY, **TG)
    out = handler(telegram_rig)("list", session_id="s-new", session_key=TG_KEY, **TG)
    assert rule_id in out and "note:" not in out


def test_unlearned_old_session_rule_stays_private(telegram_rig):
    watch(telegram_rig, session_id="s-old")
    out = handler(telegram_rig)("list", session_id="s-new", session_key=TG_KEY, **TG)
    assert "No watch rules yet" in out


def test_hint_when_rule_is_matched_by_learned_route_only(telegram_rig):
    rule_id = watch(telegram_rig, session_id="s-old")
    telegram_rig.sentinel.routes.remember(
        {"session_id": "s-old", "session_key": TG_KEY, "platform": "telegram", "chat_id": "42"}
    )
    out = handler(telegram_rig)("list", session_id="s-new", session_key="k-new", **TG)
    assert rule_id in out and "earlier session of this chat" in out and "/sentinel here" in out
    tool = telegram_rig.ctx.tools["sentinel_rules"]["handler"]
    rules = json.loads(tool({}, session_id="s-new", session_key="k-new", **TG))["rules"]
    assert "earlier session of this chat" in rules[0]["hint"]
