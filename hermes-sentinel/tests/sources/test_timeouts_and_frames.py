"""Timeout helper and frame normalisation."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import image, png_bytes

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


def test_call_with_timeout_returns_and_raises() -> None:
    assert timeouts.call_with_timeout(lambda: 5, 1.0) == 5

    def boom() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        timeouts.call_with_timeout(boom, 1.0)


def test_call_with_timeout_abandons_and_cleans_up() -> None:
    gate = threading.Event()
    cleaned = threading.Event()
    seen: list[Any] = []

    def slow() -> str:
        gate.wait(5)
        return "late"

    def cleanup(value: str) -> None:
        seen.append(value)
        cleaned.set()

    start = time.monotonic()
    with pytest.raises(timeouts.CallTimeoutError):
        timeouts.call_with_timeout(slow, 0.1, on_abandoned=cleanup)
    assert time.monotonic() - start < 1.0
    gate.set()
    assert cleaned.wait(2)
    assert seen == ["late"]


# ----------------------------------------------------------------------------- frames


def test_as_bgr_conversions() -> None:
    gray = np.full((4, 5), 7, dtype=np.uint8)
    out = frames.as_bgr(gray, origin="t")
    assert out.shape == (4, 5, 3) and out.dtype == np.uint8
    one = np.zeros((4, 5, 1), dtype=np.uint8)
    assert frames.as_bgr(one, origin="t").shape == (4, 5, 3)
    bgra = np.zeros((4, 5, 4), dtype=np.uint8)
    bgra[..., 3] = 255
    out = frames.as_bgr(bgra, origin="t")
    assert out.shape == (4, 5, 3) and out.flags.c_contiguous
    deep = np.full((2, 2, 3), 0xFFFF, dtype=np.uint16)
    assert frames.as_bgr(deep, origin="t").max() == 255
    for bad in (
        None,
        np.zeros((0, 0, 3), np.uint8),
        np.zeros((2, 2, 3), np.float32),
        np.zeros((2, 2, 2), np.uint8),
        np.zeros((2,), np.uint8),
    ):
        with pytest.raises(SourceError):
            frames.as_bgr(bad, origin="t")


def test_decode_image_errors() -> None:
    with pytest.raises(SourceError):
        frames.decode_image(b"", origin="t")
    with pytest.raises(SourceError, match="not a decodable"):
        frames.decode_image(b"not an image", origin="t")
    img = frames.decode_image(png_bytes(image(color=(1, 2, 3))), origin="t")
    assert tuple(img[0, 0]) == (1, 2, 3)
