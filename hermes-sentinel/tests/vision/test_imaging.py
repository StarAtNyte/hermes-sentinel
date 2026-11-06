"""Image encoding, downscaling and ROI cropping."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from harness.judge_helpers import frame
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
imaging_mod = plugin_module("sentinel.vision.imaging")
Judge = judge_mod.Judge
Example = judge_mod.Example


# --------------------------------------------------------------------------- encoding


def test_encode_jpeg_limits_longest_side():
    data, mime = imaging_mod.encode_image(np.zeros((1500, 2000, 3), np.uint8))
    assert mime == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (768, 1024)


def test_small_frames_are_not_upscaled():
    data, _ = imaging_mod.encode_image(frame(160, 120))
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (120, 160)


@pytest.mark.parametrize(
    "arr",
    [
        np.full((40, 50), 7, np.uint8),
        np.full((40, 50, 1), 7, np.uint8),
        np.full((40, 50, 4), 7, np.uint8),
        np.full((40, 50, 3), 0.5, np.float32),
        np.full((40, 50, 3), 1000, np.uint16),
        np.full((40, 50, 3), np.nan, np.float64),
    ],
)
def test_encode_accepts_common_layouts(arr):
    data, mime = imaging_mod.encode_image(arr)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (40, 50, 3)


@pytest.mark.parametrize("bad", [np.zeros((4,)), np.zeros((0, 5, 3)), np.zeros((5, 5, 2)), "x"])
def test_encode_rejects_unusable_frames(bad):
    with pytest.raises(ValueError):
        imaging_mod.encode_image(bad)
    assert imaging_mod.encode_jpeg(bad) is None


def test_png_fallback_without_cv2(monkeypatch):
    monkeypatch.setattr(imaging_mod, "_cv2", lambda: None)
    src = np.random.default_rng(0).integers(0, 255, (30, 41, 3), dtype=np.uint8)
    data, mime = imaging_mod.encode_image(src)
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(decoded, src)  # lossless, BGR order preserved
    assert imaging_mod.encode_jpeg(src) is None  # storage needs JPEG


def test_numpy_downscale_fallback(monkeypatch):
    monkeypatch.setattr(imaging_mod, "_cv2", lambda: None)
    data, _ = imaging_mod.encode_image(np.zeros((900, 3000, 3), np.uint8), max_side=1024)
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) <= 1024


def test_crop_roi():
    img = frame(200, 100)
    assert imaging_mod.crop_roi(img, None) is None
    assert imaging_mod.crop_roi(img, (0, 0, 1, 1)) is None  # most of the frame: no close-up
    crop = imaging_mod.crop_roi(img, (0.5, 0.5, 0.5, 0.5))
    assert crop.shape == (50, 100, 3)
    assert imaging_mod.crop_roi(img, (0.0, 0.0, 0.01, 0.01)) is None  # too small
    assert imaging_mod.crop_roi(img, ("a", 0, 1, 1)) is None
    assert imaging_mod.crop_roi(img, (0, 0, float("nan"), 0.2)) is None
