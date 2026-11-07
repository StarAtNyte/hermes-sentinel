"""``sentinel_teach``."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from harness.tool_calls import fire_event, watch


def test_teach_from_files_event_and_source(rig, tmp_path):
    rule_id = watch(rig)["rule"]["rule_id"]
    img = np.full((30, 40, 3), 90, np.uint8)
    jpg, png = tmp_path / "a.jpg", tmp_path / "b.png"
    cv2.imwrite(str(jpg), img)
    cv2.imwrite(str(png), img)
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", image_path=str(jpg))
    assert out["ok"] and out["examples"] == {"yes": 1, "no": 0}
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="no", image_path=str(png), note="me")
    assert out["ok"] and out["examples"] == {"yes": 1, "no": 1}
    ev = fire_event(rig, rule_id)
    assert rig.call("sentinel_teach", rule_id=rule_id, kind="no", event_id=ev.id)["ok"]
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", from_source=True)
    assert out["ok"] and out["examples"] == {"yes": 2, "no": 2}


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({}, "exactly one"),
        ({"image_path": "/x.jpg", "event_id": 1}, "exactly one"),
        ({"image_path": "/nope/missing.jpg"}, "no image file"),
        ({"image_path": "/etc/passwd"}, "must be a .jpg"),
        ({"event_id": 4242}, "no event #4242"),
        ({"kind": "maybe", "event_id": 1}, "kind must be"),
    ],
)
def test_teach_errors(rig, extra, needle):
    rule_id = watch(rig)["rule"]["rule_id"]
    args = {"rule_id": rule_id, "kind": "yes", **extra}
    out = rig.call("sentinel_teach", **args)
    assert out["ok"] is False and needle in out["error"], out


def test_teach_rejects_non_images(rig, tmp_path):
    rule_id = watch(rig)["rule"]["rule_id"]
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"not an image at all")
    out = rig.call("sentinel_teach", rule_id=rule_id, kind="yes", image_path=str(fake))
    assert out["ok"] is False and "not an image" in out["error"]
