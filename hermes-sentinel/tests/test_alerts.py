"""Alerter routing, fallbacks, rate limits, message format and origin capture."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from harness.fake_ctx import FakeCtx
from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.store")


def rule(**kw):
    base = {"question": "Is the mug gone?", "source": "camera:0", "text": "mug taken",
            "id": "r1"}
    base.update(kw)
    return rules.RuleSpec(**base).validate()


def event(**kw):
    base = dict(id=7, rule_id="r1", ts="2026-11-06T14:03:11.000Z", caption="mug taken",
                reason="the desk is empty", confidence=0.92, source="camera:0",
                snapshot_path="/data/snap.jpg", feedback=None)
    base.update(kw)
    return store_mod.EventRecord(**base)


@pytest.fixture
def ctx():
    c = FakeCtx()
    yield c
    c.close()


# --------------------------------------------------------------------------- routing


def test_session_key_route(ctx):
    a = alerts.Alerter(ctx)
    d = a.alert_event(rule(origin={"session_key": "agent:main:telegram:dm:1"}), event(),
                      source_name="camera:0")
    assert d.ok and d.via == "session_key"
    msg = ctx.injected[-1]
    assert msg.session_key == "agent:main:telegram:dm:1" and msg.origin is None
    assert msg.role == "user"


def test_origin_route_for_messaging_chats(ctx):
    origin = {"platform": "telegram", "chat_id": "42", "user_id": "u1", "session_id": "s1",
              "surface": "telegram"}
    d = alerts.Alerter(ctx).alert_event(rule(origin=origin), event(), source_name="cam")
    assert d.via == "origin"
    assert ctx.injected[-1].origin == {"platform": "telegram", "chat_id": "42", "user_id": "u1"}


@pytest.mark.parametrize("origin", [{}, {"platform": "cli", "chat_id": "x"}, {"surface": "tui"}])
def test_local_sessions_use_keyless_injection(ctx, origin):
    d = alerts.Alerter(ctx).alert_event(rule(origin=origin), event(), source_name="cam")
    assert d.via == "keyless"
    assert ctx.injected[-1].session_key is None and ctx.injected[-1].origin is None


def test_refused_injection_falls_back_to_post(ctx):
    ctx.inject_result = False
    d = alerts.Alerter(ctx).alert_event(rule(), event(), source_name="cam", post_fallback=True)
    assert d.ok and d.via == "post"
    assert "MEDIA:/data/snap.jpg" in ctx.posted[-1].text


def test_no_post_fallback_from_background_threads(ctx):
    ctx.inject_result = False
    d = alerts.Alerter(ctx).alert_event(rule(), event(), source_name="cam")
    assert not d.ok and d.via == "failed" and len(ctx.posted) == 0


def test_post_fallback_never_for_chat_origins(ctx):
    ctx.inject_result = False
    origin = {"platform": "telegram", "chat_id": "42", "surface": "telegram"}
    d = alerts.Alerter(ctx).alert_event(rule(origin=origin), event(), source_name="cam",
                                        post_fallback=True)
    assert not d.ok and len(ctx.posted) == 0


def test_both_paths_failing_is_reported_not_raised(ctx):
    ctx.inject_result = False
    ctx.post_ok = False
    a = alerts.Alerter(ctx)
    d = a.alert_event(rule(), event(), source_name="cam", post_fallback=True)
    assert not d.ok and d.via == "failed" and "no_conversation" in d.detail
    assert a.stats()["failed"] == 1


def test_raising_host_calls_are_contained():
    class Boom:
        def inject_message(self, *a, **k):
            raise RuntimeError("gateway gone")

        def post_message(self, text):
            raise RuntimeError("no session")

    d = alerts.Alerter(Boom()).notice("hello", post_fallback=True)
    assert not d.ok and d.via == "failed"

    class InjectBoom(Boom):
        def post_message(self, text):
            return SimpleNamespace(ok=True)

    assert alerts.Alerter(InjectBoom()).notice("hello", post_fallback=True).via == "post"


# --------------------------------------------------------------------------- rate limits


def test_per_rule_min_interval_and_suppressed_count(ctx):
    clock = FakeClock()
    a = alerts.Alerter(ctx, clock=clock, per_rule_min_interval=30)
    r = rule()
    assert a.alert_event(r, event(id=1), source_name="cam").ok
    clock.advance(10)
    assert a.alert_event(r, event(id=2), source_name="cam").via == "suppressed"
    assert a.alert_event(rule(id="r2"), event(id=3), source_name="cam").ok  # other rule
    clock.advance(25)
    assert a.alert_event(r, event(id=4), source_name="cam").ok
    assert "1 earlier alert(s) were rate-limited" in ctx.injected[-1].content
    assert a.stats() == {"sent": 3, "suppressed": 1, "failed": 0}


def test_global_hourly_cap(ctx):
    clock = FakeClock()
    a = alerts.Alerter(ctx, clock=clock, per_rule_min_interval=0, global_per_hour=3)
    results = [a.alert_event(rule(id=f"r{i}"), event(id=i), source_name="c").ok
               for i in range(5)]
    assert results == [True, True, True, False, False]
    clock.advance(3600)
    assert a.alert_event(rule(id="r9"), event(id=9), source_name="c").ok


def test_notice_cap_and_format(ctx):
    clock = FakeClock()
    a = alerts.Alerter(ctx, clock=clock, notices_per_hour=2)
    assert a.notice("Source camera:0 is offline.").ok
    assert a.notice("again").ok
    assert a.notice("third").via == "suppressed"
    assert ctx.injected[0].content.startswith("[Sentinel notice] Source camera:0 is offline.")
    clock.advance(3601)
    assert a.notice("later").ok


def test_zero_caps_suppress_everything(ctx):
    a = alerts.Alerter(ctx, global_per_hour=0, notices_per_hour=0)
    assert a.alert_event(rule(), event(), source_name="c").via == "suppressed"
    assert a.notice("x").via == "suppressed"
    assert list(ctx.injected) == []


# --------------------------------------------------------------------------- message


def test_message_contents():
    text = alerts.format_event_message(rule(), event(), source_name="camera:0")
    lines = text.splitlines()
    assert lines[0] == "[Sentinel alert] Rule r1 fired on camera:0 (confidence 0.92)."
    assert 'Rule r1 ("mug taken"). Event #7' in lines[1]
    assert "> Caption: mug taken" in lines and "> Evidence: the desk is empty" in lines
    assert "MEDIA:/data/snap.jpg" in lines
    assert "event_id=7" in text and "false_alarm" in text
    assert "carry out" not in text


def test_message_without_media_and_with_action():
    r = rule(action="agent", action_prompt="turn on the porch light")
    text = alerts.format_event_message(r, event(snapshot_path=None, confidence=None,
                                                caption="", reason=""),
                                       source_name="cam")
    assert "MEDIA:" not in text and "confidence" not in text.splitlines()[0]
    assert "the watch condition was seen" in text
    lines = text.splitlines()
    assert "Then carry out the rule's action: turn on the porch light" in lines
    assert lines.index("Then carry out the rule's action: turn on the porch light") < lines.index(
        "--- BEGIN UNTRUSTED OBSERVATION ---")
    assert "MEDIA:" not in alerts.format_event_message(rule(), event(), source_name="c",
                                                       include_media=False)


def test_message_is_bounded():
    text = alerts.format_event_message(rule(text="x" * 2000), event(caption="y" * 5000),
                                       source_name="c")
    assert len(text) <= alerts.MAX_MESSAGE_LEN


def test_alerter_include_media_default_and_override(ctx):
    a = alerts.Alerter(ctx, include_media=False)
    a.alert_event(rule(), event(), source_name="c")
    assert "MEDIA:" not in ctx.injected[-1].content
    a.alert_event(rule(id="r2"), event(), source_name="c", include_media=True)
    assert "MEDIA:" in ctx.injected[-1].content


def test_bad_rule_object_still_alerts(ctx):
    broken = SimpleNamespace(id="rx", origin={})  # no text/question attributes
    d = alerts.Alerter(ctx).alert_event(broken, event(), source_name="c")
    assert d.ok and "rule rx fired" in ctx.injected[-1].content


# --------------------------------------------------------------------------- origin capture


def test_local_session_id_is_used_as_session_key(ctx):
    a = alerts.Alerter(ctx)
    d = a.alert_event(rule(origin={"session_id": "ses_1", "surface": "tui"}), event(),
                      source_name="c")
    assert d.via == "session_id" and ctx.injected[-1].session_key == "ses_1"


def test_routes_are_tried_in_order_until_accepted():
    class Picky:
        def __init__(self):
            self.tried = []

        def inject_message(self, text, role="user", *, session_key=None, origin=None):
            self.tried.append(session_key or (origin and "origin") or "keyless")
            return session_key is None and origin is None  # only key-less is accepted

        def post_message(self, text):
            raise AssertionError("not reached")

    host = Picky()
    origin = {"session_key": "k", "platform": "telegram", "chat_id": "1", "session_id": "s"}
    d = alerts.Alerter(host).alert_event(rule(origin=origin), event(), source_name="c")
    assert d.via == "failed" and host.tried == ["k", "origin"]  # chat: never key-less
    host.tried.clear()
    alerts.Alerter(host).notice("x", origin={"session_id": "s", "surface": "desktop"})
    assert host.tried == ["s", "keyless"]


def test_capture_origin_from_kwargs_and_surface():
    ctx = FakeCtx(session_surface="tui")
    try:
        out = alerts.capture_origin({"task_id": "t", "session_id": "ses_9"}, ctx)
    finally:
        ctx.close()
    assert out == {"session_id": "ses_9", "surface": "tui"}


def test_capture_origin_uses_host_keywords_and_source():
    src = SimpleNamespace(chat_id="c1", user_id="u1", thread_id=None,
                          platform=SimpleNamespace(value="slack"))
    out = alerts.capture_origin({"session_key": "k1", "session_id": "s1", "source": src})
    assert out == {"session_key": "k1", "session_id": "s1", "chat_id": "c1", "user_id": "u1",
                   "platform": "slack"}
    out = alerts.capture_origin({"source": {"chat_id": "d1", "platform": "discord"}})
    assert out == {"chat_id": "d1", "platform": "discord"}


def test_capture_origin_is_safe_and_valid_for_rules():
    class BadCtx:
        def session_surface(self):
            raise RuntimeError("x")

    out = alerts.capture_origin({"session_id": "x" * 1000, "session_key": True,
                                 "platform": ["x"]}, BadCtx())
    assert out == {}
    assert alerts.capture_origin(None) == {}
    rule(origin=alerts.capture_origin({"session_id": "s", "source": object()}))


def test_route_book_remembers_and_persists(tmp_path):
    path = tmp_path / "routes.json"
    book = alerts.RouteBook(path, limit=2)
    tg = SimpleNamespace(platform="telegram", chat_id="42", user_id="u")
    route = book.remember({"session_id": "s1", "session_key": "agent:tg:42", "source": tg})
    assert route == {"session_key": "agent:tg:42", "platform": "telegram", "chat_id": "42",
                     "user_id": "u"}
    assert book.lookup("s1") == route and book.lookup("nope") == {} and book.lookup(None) == {}
    assert book.remember({"session_id": "s0"}) == {}  # nothing routable: not stored
    book.remember({"session_key": "ses_local"})  # local: key doubles as the session id
    book.remember({"session_id": "s3", "session_key": "k3"})  # evicts the oldest (s1)
    again = alerts.RouteBook(path, limit=2)
    assert again.lookup("s1") == {} and again.lookup("s3") == {"session_key": "k3"}
    assert again.lookup("ses_local") == {"session_key": "ses_local"}
    out = alerts.capture_origin({"session_id": "s3"}, routes=again)
    assert out == {"session_id": "s3", "session_key": "k3"}


def test_route_book_survives_bad_files(tmp_path):
    bad = tmp_path / "routes.json"
    bad.write_text("{not json")
    assert alerts.RouteBook(bad).lookup("x") == {}
    bad.write_text('["a list"]')
    assert alerts.RouteBook(bad).lookup("x") == {}
    bad.write_text('{"s": {"session_key": "k"}, "t": 5, "u": {"junk": 1}}')
    book = alerts.RouteBook(bad)
    assert book.lookup("s") == {"session_key": "k"} and book.lookup("u") == {}
    unwritable = alerts.RouteBook(tmp_path / "f" / "sub")
    (tmp_path / "f").write_text("a file, not a dir")
    assert unwritable.remember({"session_key": "k"}) == {"session_key": "k"}  # no raise
    assert alerts.RouteBook(None).remember({"session_key": "k"})
