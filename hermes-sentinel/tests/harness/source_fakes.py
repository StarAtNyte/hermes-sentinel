"""Fakes for OpenCV and mss used by the frame-source tests (no camera or display needed)."""

from __future__ import annotations

import os
import threading
import types
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def image(h: int = 48, w: int = 64, color: tuple[int, int, int] = (10, 20, 30)) -> np.ndarray:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


class FakeCapture:
    """Stand-in for cv2.VideoCapture."""

    instances: list[FakeCapture] = []

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    def __init__(
        self,
        target: Any,
        *args: Any,
        frames: list[Any] | None = None,
        opened: bool = True,
        block: threading.Event | None = None,
    ) -> None:
        if block is not None:
            block.wait(10)
        self.target = target
        self.args = args
        self.frames = list(frames) if frames is not None else [image()]
        self.opened = opened
        self.released = 0
        self.props: dict[int, float] = {}
        FakeCapture.instances.append(self)

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2
        return self.opened

    def read(self) -> tuple[bool, Any]:
        if not self.frames:
            return False, None
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return True, item

    def set(self, prop: int, value: float) -> bool:
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return {3: 640.0, 4: 480.0}.get(prop, 0.0)

    def release(self) -> None:
        self.released += 1
        self.opened = False


def make_fake_cv2(factory) -> types.ModuleType:
    mod = types.ModuleType("cv2_fake")
    mod.VideoCapture = factory
    mod.CAP_PROP_BUFFERSIZE = 38
    mod.CAP_PROP_FRAME_WIDTH = 3
    mod.CAP_PROP_FRAME_HEIGHT = 4
    mod.CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    mod.CAP_PROP_READ_TIMEOUT_MSEC = 54
    mod.CAP_FFMPEG = 1900
    mod.IMREAD_COLOR = cv2.IMREAD_COLOR
    mod.imdecode = cv2.imdecode
    return mod


class FakeMSS:
    created: list[FakeMSS] = []

    @classmethod
    def reset(cls) -> None:
        cls.created = []

    monitors = [
        {"left": 0, "top": 0, "width": 300, "height": 100},
        {"left": 0, "top": 0, "width": 200, "height": 100},
        {"left": 200, "top": 0, "width": 100, "height": 80},
    ]

    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.grabs: list[dict[str, int]] = []
        self.closed = False
        self.fail = False
        FakeMSS.created.append(self)

    def grab(self, box: dict[str, int]) -> np.ndarray:
        if self.fail:
            raise RuntimeError("XGetImage failed")
        self.grabs.append(dict(box))
        img = np.zeros((box["height"], box["width"], 4), dtype=np.uint8)
        img[..., 0] = 11
        img[..., 3] = 255
        return img

    def close(self) -> None:
        self.closed = True


def write_image(path: Path, img: np.ndarray, mtime: float) -> None:
    """Write ``img`` to ``path`` and set its modification time."""
    assert cv2.imwrite(str(path), img)
    os.utime(path, (mtime, mtime))


__all__ = ["FakeCapture", "FakeMSS", "image", "make_fake_cv2", "png_bytes", "write_image"]
