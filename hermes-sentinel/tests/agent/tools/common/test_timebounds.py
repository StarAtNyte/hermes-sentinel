"""Time bound parsing for event searches."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from harness.plugin_loader import plugin_module

tools = plugin_module("sentinel.agent.tools")


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
