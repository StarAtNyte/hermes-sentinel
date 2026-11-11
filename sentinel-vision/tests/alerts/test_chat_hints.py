"""Chat-session matching: route-completed origins, visibility and the "run /sentinel here" hint."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")

CHAT_CALLER = {"platform": "telegram", "chat_id": "42", "session_id": "new", "surface": "telegram"}
KEY = {"session_key": "k42", "platform": "telegram", "chat_id": "42"}


def book_with(session_id, route):
    book = alerts.RouteBook()
    book.remember({"session_id": session_id, **route})
    return book


def test_has_route():
    assert alerts.has_route({"session_key": "k"})
    assert alerts.has_route({"platform": "telegram", "chat_id": "1"})
    assert not alerts.has_route({"session_id": "s", "surface": "telegram"})
    assert not alerts.has_route({"platform": "cli", "chat_id": "1"})


def test_effective_origin_fills_from_route_book_without_overriding():
    book = book_with("old", KEY)
    merged = alerts.effective_origin({"session_id": "old", "chat_id": "7"}, book)
    assert merged["chat_id"] == "7" and merged["session_key"] == "k42"
    assert alerts.effective_origin({"session_id": "old"}, None) == {"session_id": "old"}


def test_old_session_rule_becomes_visible_once_its_chat_is_learned():
    origin = {"session_id": "old", "surface": "telegram"}
    assert not alerts.visible_to(origin, CHAT_CALLER, alerts.RouteBook())
    assert alerts.visible_to(origin, CHAT_CALLER, book_with("old", KEY))


def test_another_chat_stays_invisible():
    origin = {"session_id": "old", "surface": "telegram"}
    other = {**KEY, "chat_id": "99", "session_key": "k99"}
    assert not alerts.visible_to(origin, CHAT_CALLER, book_with("old", other))


def test_route_hint_wording_and_scope():
    earlier = alerts.route_hint({"session_id": "old"}, CHAT_CALLER)
    assert "earlier session of this chat" in earlier and "/sentinel here" in earlier
    this = alerts.route_hint({"session_id": "new"}, CHAT_CALLER)
    assert "in this session" in this
    assert alerts.route_hint({"session_id": "old", **KEY}, CHAT_CALLER) == ""
    assert alerts.route_hint({"session_id": "old"}, {"surface": "cli"}) == ""
