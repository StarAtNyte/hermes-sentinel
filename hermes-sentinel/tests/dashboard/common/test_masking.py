"""Credential masking of source specs."""

from __future__ import annotations

import pytest
from harness.dashboard_rig import (
    api_module,
)


@pytest.mark.parametrize(
    "spec,secret",
    [
        ("snapshot:https://u:pw@h.example/x.jpg", "pw"),
        ("https://tok123@h.example/x", "tok123"),
        ("folder:https://tok123@h.example/x", "tok123"),
        ("rtsp://h.example/live/SECRETPATH", "SECRETPATH"),
        ("rtsp://h.example:554/Streaming/s3cr3t-tok/ch1?x=1", "s3cr3t-tok"),
    ],
)
def test_mask_hides_credential_shapes(api, rig, spec, secret):
    out = api_module(api, "common.masking").mask(spec)
    assert secret not in out
    assert "://" in out


def test_mask_keeps_placeholders_and_generic_paths(api, rig):
    assert (
        api_module(api, "common.masking").mask("https://${CAM_USER}@h.example/x")
        == "https://${CAM_USER}@h.example/x"
    )
    assert (
        api_module(api, "common.masking")
        .mask("snapshot:https://u:${CAM_PW}@h.example/x.jpg")
        .startswith("snapshot:https://")
    )
    assert "${CAM_PW}" in api_module(api, "common.masking").mask(
        "snapshot:https://u:${CAM_PW}@h.example/x.jpg"
    )
    assert api_module(api, "common.masking").mask("rtsp://nvr/ch1") == "rtsp://nvr/ch1"
    assert api_module(api, "common.masking").mask("camera:0") == "camera:0"
    # two cameras behind one recorder stay distinguishable
    a = api_module(api, "common.masking").mask("rtsp://nvr/secretAAAA")
    b = api_module(api, "common.masking").mask("rtsp://nvr/secretBBBB")
    assert a != b
