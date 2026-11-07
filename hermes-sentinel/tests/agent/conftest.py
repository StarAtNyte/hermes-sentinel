"""Fixtures for the agent layer tests: a Sentinel on a fake context with one static camera."""

from __future__ import annotations

import pytest
from harness.fake_sources import FakeSource
from harness.scenes import build_scene
from sentinel_rig import Rig, verdict

CAM = "camera:7"


@pytest.fixture
def rig(fake_ctx):
    r = Rig(fake_ctx)
    r.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=20), r.clock, loop=True)
    fake_ctx.llm.respond(lambda call: verdict(False, 0.9, "empty desk"), task="sentinel_vision")
    return r
