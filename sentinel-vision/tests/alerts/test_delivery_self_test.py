"""Delivery self-test: one small injected message, honest result, sticky state."""

from __future__ import annotations

import pytest
from harness.fake_ctx import FakeCtx
from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")

CHAT = {"platform": "telegram", "chat_id": "42", "session_key": "agent:main:telegram:dm:42"}


@pytest.fixture
def make_alerter():
    made = []

    def build(clock=None, **ctx_kwargs):
        ctx = FakeCtx(**ctx_kwargs)
        made.append(ctx)
        return ctx, alerts.Alerter(ctx, **({"clock": clock} if clock else {}))

    yield build
    for ctx in made:
        ctx.close()


def test_accepted_test_marks_delivery_verified(make_alerter):
    ctx, alerter = make_alerter(session_surface="telegram")
    result = alerts.run_delivery_test(alerter, CHAT)
    assert result.delivered and result.via == "origin"
    assert ctx.injected[-1].origin["chat_id"] == "42"
    assert "delivery test" in ctx.injected[-1].content.lower()
    assert alerter.delivery_verified() and alerter.delivery_blocked() is None
    assert alerter.stats() == {"sent": 0, "suppressed": 0, "failed": 0}  # not an alert


def test_refused_test_sets_sticky_blocked_and_reports_yaml(make_alerter):
    ctx, alerter = make_alerter(session_surface="telegram", gateway_injection_allowed=False)
    result = alerts.run_delivery_test(alerter, CHAT)
    assert not result.delivered
    assert alerter.delivery_blocked() and not alerter.delivery_verified()
    text = alerts.format_delivery_test(result)
    assert "BLOCKED" in text and alerts.GATEWAY_INJECTION_YAML in text


def test_later_success_clears_blocked(make_alerter):
    now = [0.0]
    ctx, alerter = make_alerter(
        clock=lambda: now[0], session_surface="telegram", gateway_injection_allowed=False
    )
    alerts.run_delivery_test(alerter, CHAT)
    ctx.gateway_injection_allowed = True
    now[0] += 61.0
    result = alerts.run_delivery_test(alerter, CHAT)
    assert result.delivered and alerter.delivery_blocked() is None
    assert "delivered" in alerts.format_delivery_test(result)


def test_chat_without_route_is_not_reported_as_blocked(make_alerter):
    _, alerter = make_alerter(session_surface="telegram")
    result = alerts.run_delivery_test(alerter, {"surface": "telegram", "session_id": "s1"})
    assert not result.delivered
    text = alerts.format_delivery_test(result)
    assert "not sent" in text and "BLOCKED" not in text
    assert alerter.delivery_blocked() is None
