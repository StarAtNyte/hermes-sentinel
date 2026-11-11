"""Screen source over a fake mss."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from harness.plugin_loader import plugin_module

src = plugin_module("sentinel.sources")
deps = plugin_module("sentinel.sources.dependencies")
timeouts = plugin_module("sentinel.sources.timeouts")
frames = plugin_module("sentinel.sources.frames")
screen = plugin_module("sentinel.sources.backends.screen")
discovery = plugin_module("sentinel.sources.discovery")
SourceSpec = src.SourceSpec
SourceSpecError = src.SourceSpecError
SourceError = src.SourceError
SourceDisconnected = src.SourceDisconnected
SourceNotReady = src.SourceNotReady
MissingDependency = src.MissingDependency


def test_screen_whole_monitor(fake_mss) -> None:
    screen = src.open_source("screen:2", clock=lambda: 7.0)
    assert isinstance(screen, src.ScreenSource)
    screen.open()
    ts, frame = screen.read()
    assert ts == 7.0 and frame.shape == (80, 100, 3) and frame[0, 0, 0] == 11
    assert fake_mss.created[0].grabs == [{"left": 200, "top": 0, "width": 100, "height": 80}]
    screen.close()
    assert fake_mss.created[0].closed
    screen.close()


def test_screen_region_clipped(fake_mss) -> None:
    with src.open_source("screen:2@50,10,100,100") as screen:
        _, frame = screen.read()
    assert fake_mss.created[0].grabs == [{"left": 250, "top": 10, "width": 50, "height": 70}]
    assert frame.shape == (70, 50, 3)


def test_screen_region_outside(fake_mss) -> None:
    screen = src.open_source("screen:1@500,500,10,10")
    with pytest.raises(SourceSpecError, match="outside"):
        screen.open()
    assert not screen.is_open and fake_mss.created[0].closed


def test_screen_missing_monitor(fake_mss) -> None:
    with pytest.raises(SourceDisconnected, match="monitor 5 not found"):
        src.open_source("screen:5").open()


def test_screen_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory() -> Any:
        raise RuntimeError("$DISPLAY not set")

    monkeypatch.setattr(screen, "mss_factory", lambda p: (factory, RuntimeError))
    with pytest.raises(SourceDisconnected, match="graphical session"):
        src.open_source("screen").open()


def test_screen_thread_affinity(fake_mss) -> None:
    screen = src.open_source("screen:1")
    screen.open()
    screen.read()
    result: list[Any] = []
    t = threading.Thread(target=lambda: result.append(screen.read()))
    t.start()
    t.join(2)
    assert result
    assert len(fake_mss.created) == 2
    assert fake_mss.created[0].closed
    assert fake_mss.created[1].thread != fake_mss.created[0].thread
    screen.close()
    assert fake_mss.created[1].closed


def test_screen_grab_failure(fake_mss) -> None:
    screen = src.open_source("screen:1")
    screen.open()
    fake_mss.created[0].fail = True
    with pytest.raises(SourceDisconnected, match="grab failed"):
        screen.read()
    # The handle is recreated on the next read.
    _, frame = screen.read()
    assert frame.shape == (100, 200, 3)
    screen.close()


def test_real_mss_factory_resolves() -> None:
    factory, err = screen.mss_factory("test")
    assert callable(factory)
    assert issubclass(err, BaseException)
