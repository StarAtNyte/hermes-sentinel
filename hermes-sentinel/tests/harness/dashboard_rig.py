"""Helpers for the dashboard API tests: loading ``plugin_api.py`` the way Hermes does."""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.plugin_loader import PLUGIN_DIR, plugin_module, plugin_package

CAM = "camera:7"
RTSP = "rtsp://admin:hunter2@10.0.0.5:554/stream?token=abc123"
SECRETS = ("hunter2", "abc123")
BASE = "/api/plugins/hermes-sentinel"


def load_api(name: str = "hermes_dashboard_plugin_hermes_sentinel") -> Any:
    """Import plugin_api.py the way Hermes does: standalone, by file path."""
    plugin_package()
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def api_module(api: Any, dotted: str) -> Any:
    """A module of the plugin's ``dashboard_api`` package, e.g. ``"common.masking"``."""
    return plugin_module(f"sentinel.dashboard_api.{dotted}")


def make_client(api: Any) -> TestClient:
    """A test client with the dashboard router mounted where Hermes mounts it."""
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def jpeg() -> bytes:
    """A tiny valid JPEG."""
    ok, buf = cv2.imencode(".jpg", np.full((40, 60, 3), 90, np.uint8))
    assert ok
    return bytes(buf)


def watch_rule(rig, **args) -> str:
    """Create a desk-intruder rule on ``CAM`` through the rig; returns its id."""
    args.setdefault("recipe", "desk-intruder")
    args.setdefault("source", CAM)
    return rig.watch(**args)["rule"]["rule_id"]


def add_event(rig, rule_id, caption="someone at the desk", snapshot=True, **kw):
    """Store an event (with a photo unless ``snapshot`` is false) for ``rule_id``."""
    store = rig.sentinel.store
    path = store.save_snapshot(jpeg(), rule_id=rule_id) if snapshot else None
    return store.add_event(
        rule_id, caption=caption, confidence=0.91, source=CAM, snapshot_path=path, **kw
    )


def no_secrets(resp) -> None:
    """Assert the response text carries none of the credentials in ``SECRETS``."""
    for secret in SECRETS:
        assert secret not in resp.text
