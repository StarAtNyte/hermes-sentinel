"""``sentinel_look``."""

from __future__ import annotations

from harness.fake_sources import FakeSource
from harness.scenes import build_scene
from harness.tool_calls import CAM
from sentinel_rig import Rig


def test_look_describes_and_saves_photo(rig):
    rig.ctx.llm.script(
        {"answer": "An empty desk.", "caption": "empty desk"}, purpose="sentinel:look"
    )
    out = rig.call("sentinel_look", source=CAM, question="Is anyone there?")
    assert out["ok"], out
    assert out["answer"] == "An empty desk." and out["media"].startswith("MEDIA:")
    assert rig.sentinel.store.usage_totals(rule_id="look")[0] == 1


def test_look_errors(rig):
    rig.ctx.llm.script(RuntimeError("no vision"), purpose="sentinel:look")
    out = rig.call("sentinel_look", source=CAM)
    assert out["ok"] is False and "could not answer" in out["error"] and out["media"]
    out = rig.call("sentinel_look", source="camera:9")
    assert out["ok"] is False and "could not read the source" in out["error"]


def test_look_respects_budget(fake_ctx):
    rig = Rig(fake_ctx, config={"vision_calls_per_hour": 0})
    rig.sources[CAM] = FakeSource(build_scene("static_scene", n_frames=5), rig.clock)
    out = rig.call("sentinel_look", source=CAM)
    assert out["ok"] is False and "Vision checks: 0/0" in out["error"]
    assert len(fake_ctx.llm.calls) == 0
