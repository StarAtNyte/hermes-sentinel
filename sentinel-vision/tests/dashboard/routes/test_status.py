"""GET /status."""

from __future__ import annotations

from harness.dashboard_rig import (
    BASE,
    make_client,
    watch_rule,
)
from harness.plugin_loader import plugin_module
from sentinel_rig import Rig

runtime = plugin_module("sentinel.runtime")


def test_status_before_any_rule_engine_not_running(client):
    body = client.get(f"{BASE}/status").json()
    assert body["engine"]["running"] is False and body["engine"]["since"] is None
    assert body["delivery"] == {"blocked": False, "reason": None}
    assert body["budget"]["hour_used"] == 0 and body["budget"]["paused"] is False
    assert body["rules"] == 0 and body["sources"] == 0


def test_status_with_running_engine(client, rig):
    watch_rule(rig)
    rig.pump(1)
    body = client.get(f"{BASE}/status").json()
    assert body["engine"]["running"] is True and body["engine"]["since"]
    assert body["rules"] == 1 and body["sources"] == 1
    assert body["budget"]["hour_cap"] == 60 and body["budget"]["day_cap"] == 500


def test_status_reports_blocked_delivery(client, rig):
    alerter = rig.sentinel.alerter
    alerter._blocked = True
    body = client.get(f"{BASE}/status").json()
    assert body["delivery"]["blocked"] is True and body["delivery"]["reason"]


def test_status_budget_paused_when_cap_zero(api, fake_ctx):
    r = Rig(fake_ctx, config={"vision_calls_per_hour": 0})
    runtime.set_active_sentinel(r.sentinel)
    try:
        assert make_client(api).get(f"{BASE}/status").json()["budget"]["paused"] is True
    finally:
        r.sentinel.shutdown()
        runtime.set_active_sentinel(None)
