"""Fixtures shared by the frame-source tests."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import FakeCapture, FakeMSS, make_fake_cv2

deps = plugin_module("sentinel.sources.dependencies")
screen = plugin_module("sentinel.sources.backends.screen")
discovery = plugin_module("sentinel.sources.discovery")


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeCapture.reset()
    yield
    FakeCapture.reset()


@pytest.fixture
def fake_cv2(monkeypatch: pytest.MonkeyPatch):
    """Install a fake cv2 whose VideoCapture is configurable via ``state``."""
    state: dict[str, Any] = {"frames": None, "opened": True, "block": None}

    def factory(target: Any, *args: Any) -> FakeCapture:
        return FakeCapture(
            target, *args, frames=state["frames"], opened=state["opened"], block=state["block"]
        )

    mod = make_fake_cv2(factory)
    monkeypatch.setattr(deps, "require_cv2", lambda purpose: mod)
    state["module"] = mod
    return state


@pytest.fixture
def fake_mss(monkeypatch: pytest.MonkeyPatch):
    FakeMSS.reset()
    monkeypatch.setattr(screen, "mss_factory", lambda purpose: (FakeMSS, RuntimeError))
    yield FakeMSS
    FakeMSS.reset()


@pytest.fixture
def probe_cleanup():
    """Make sure no stuck probe thread leaks into another test."""
    gates: list[threading.Event] = []
    yield gates
    for g in gates:
        g.set()
    thread = discovery._probe_thread
    if thread is not None:
        thread.join(5)
