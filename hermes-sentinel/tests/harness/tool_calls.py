"""Calls into the tools through a :class:`Rig`, shared by the tool and judging tests."""

from __future__ import annotations

import cv2
import numpy as np

CAM = "camera:7"


def watch(rig, **args):
    """``sentinel_watch`` with the desk-intruder recipe on ``CAM`` unless overridden."""
    args.setdefault("recipe", "desk-intruder")
    args.setdefault("source", CAM)
    return rig.call("sentinel_watch", **args)


def fire_event(rig, rule_id):
    """Store an event with a tiny photo for ``rule_id``."""
    jpeg = cv2.imencode(".jpg", np.zeros((20, 20, 3), np.uint8))[1].tobytes()
    path = rig.sentinel.store.save_snapshot(jpeg, rule_id=rule_id)
    return rig.sentinel.store.add_event(rule_id, caption="x", snapshot_path=path)
