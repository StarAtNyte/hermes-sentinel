"""``/sentinel setup`` over a live Sentinel."""

from __future__ import annotations


def test_command_setup(rig, no_real_device_probe):
    out = rig.ctx.run_command("sentinel", "setup")
    assert out.startswith("Sentinel setup check")
    assert "Cameras and screens: No camera" in out
    assert out.splitlines()[-1].startswith("Ready: yes - ")
    assert "setup [--test-delivery] |" in rig.ctx.run_command("sentinel", "help")
