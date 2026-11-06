"""Folder source against generated images."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from harness.plugin_loader import plugin_module
from harness.source_fakes import image, write_image

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


def test_folder_newest_image(tmp_path: Path) -> None:
    now = time.time()
    write_image(tmp_path / "a.png", image(color=(1, 1, 1)), now - 30)
    write_image(tmp_path / "b.jpg", image(color=(200, 200, 200)), now - 10)
    write_image(tmp_path / "c.png", image(color=(3, 3, 3)), now - 20)
    (tmp_path / "notes.txt").write_text("not an image")
    os.utime(tmp_path / "notes.txt", (now, now))
    write_image(tmp_path / ".hidden.png", image(color=(9, 9, 9)), now)
    (tmp_path / "subdir.png").mkdir()

    folder = src.open_source(f"folder:{tmp_path}", clock=lambda: 42.0)
    assert isinstance(folder, src.FolderSource)
    folder.open()
    ts, frame = folder.read()
    assert ts == 42.0
    assert frame.shape == (48, 64, 3)
    assert abs(int(frame[0, 0, 0]) - 200) <= 3  # JPEG is lossy
    write_image(tmp_path / "d.png", image(color=(50, 60, 70)), now + 5)
    _, frame = folder.read()
    assert tuple(frame[0, 0]) == (50, 60, 70)
    folder.close()


def test_folder_caches_decoded_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_image(tmp_path / "a.png", image(), time.time())
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)))
    folder.open()
    calls: list[str] = []
    real = frames.decode_image
    monkeypatch.setattr(
        frames,
        "decode_image",
        lambda data, origin: calls.append(origin) or real(data, origin=origin),
    )
    _, f1 = folder.read()
    _, f2 = folder.read()
    assert len(calls) == 1
    f1[:] = 0  # callers get copies
    _, f3 = folder.read()
    assert np.array_equal(f2, f3) and f3.any()


def test_folder_partial_newest_falls_back(tmp_path: Path) -> None:
    now = time.time()
    write_image(tmp_path / "good.png", image(color=(5, 5, 5)), now - 10)
    folder = src.open_source({"kind": "folder", "path": str(tmp_path)})
    folder.open()
    folder.read()
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0 truncated")
    os.utime(broken, (now, now))
    _, frame = folder.read()
    assert tuple(frame[0, 0]) == (5, 5, 5)


def test_folder_only_broken_is_not_ready(tmp_path: Path) -> None:
    (tmp_path / "x.jpg").write_bytes(b"junk")
    with src.open_source(f"folder:{tmp_path}") as folder, pytest.raises(SourceNotReady):
        folder.read()


def test_folder_empty_and_missing(tmp_path: Path) -> None:
    folder = src.open_source(f"folder:{tmp_path}")
    folder.open()
    with pytest.raises(SourceNotReady, match="no images"):
        folder.read()
    folder.close()
    missing = src.open_source(f"folder:{tmp_path / 'nope'}")
    with pytest.raises(SourceDisconnected, match="not a directory"):
        missing.open()
    gone = tmp_path / "gone"
    gone.mkdir()
    g = src.open_source(f"folder:{gone}")
    g.open()
    gone.rmdir()
    with pytest.raises(SourceDisconnected, match="no longer exists"):
        g.read()


def test_folder_size_cap(tmp_path: Path) -> None:
    write_image(tmp_path / "big.png", image(), time.time())
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)), max_file_bytes=10)
    folder.open()
    with pytest.raises(SourceNotReady):
        folder.read()


def test_folder_tie_break_by_name(tmp_path: Path) -> None:
    t = time.time()
    write_image(tmp_path / "a.png", image(color=(1, 1, 1)), t)
    write_image(tmp_path / "b.png", image(color=(2, 2, 2)), t)
    folder = src.FolderSource(SourceSpec(kind="folder", target=str(tmp_path)))
    folder.open()
    found = folder.newest()
    assert found is not None and found[0].name == "b.png"


def test_folder_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "pics").mkdir()
    folder = src.FolderSource(SourceSpec(kind="folder", target="~/pics"))
    assert folder.path == tmp_path / "pics"


def test_folder_concurrent_reads(tmp_path: Path) -> None:
    write_image(tmp_path / "a.png", image(color=(7, 7, 7)), time.time())
    folder = src.open_source(f"folder:{tmp_path}")
    folder.open()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                _, frame = folder.read()
                assert tuple(frame[0, 0]) == (7, 7, 7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors
