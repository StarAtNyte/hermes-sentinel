"""Fixtures for the dashboard API tests: the loaded API module and a wired Sentinel."""

from __future__ import annotations

import sys

import pytest
from harness.dashboard_rig import CAM, load_api, make_client
from harness.fake_sources import FakeSource
from harness.plugin_loader import plugin_module
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

runtime = plugin_module("sentinel.runtime")


@pytest.fixture
def api():
    mod = load_api()
    yield mod
    sys.modules.pop(mod.__name__, None)


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty desk"), task="sentinel_vision")
    runtime.set_active_sentinel(r.sentinel)
    yield r
    r.sentinel.shutdown()
    runtime.set_active_sentinel(None)


@pytest.fixture
def client(api, rig):
    return make_client(api)


@pytest.fixture
def idle_client(api):
    """No Sentinel loaded in this process."""
    runtime.set_active_sentinel(None)
    return make_client(api)
