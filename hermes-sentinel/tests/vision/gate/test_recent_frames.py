"""The gate's bounded ring buffer of recent frames."""

from __future__ import annotations

import numpy as np
import pytest
from harness.plugin_loader import plugin_module

gate_mod = plugin_module("sentinel.vision.gate")
buffer_mod = plugin_module("sentinel.vision.gate.recent_frames")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
RecentFrames = buffer_mod.RecentFrames
subsample = buffer_mod.subsample


def _frame(value: int = 100, shape: tuple[int, int] = (24, 32)) -> np.ndarray:
    return np.full((*shape, 3), value, dtype=np.uint8)


def test_keeps_newest_frames_spaced_apart() -> None:
    buf, cfg = RecentFrames(), GateConfig(recent_frames=3, recent_spacing_seconds=0.5)
    kept = [buf.offer(_frame(i), i * 0.2, cfg) for i in range(10)]
    assert kept[:4] == [True, False, False, True]
    stamps = [item.ts for item in buf.latest()]
    assert stamps == sorted(stamps) and len(stamps) == 3
    assert all(b - a >= 0.5 - 1e-9 for a, b in zip(stamps, stamps[1:], strict=False))


def test_latest_count_returns_the_newest_oldest_first() -> None:
    buf, cfg = RecentFrames(), GateConfig(recent_frames=5, recent_spacing_seconds=0.0)
    for i in range(5):
        buf.offer(_frame(i), float(i), cfg)
    assert [item.ts for item in buf.latest(2)] == [3.0, 4.0]
    assert buf.latest(0) == []
    assert len(buf.latest(99)) == 5


def test_byte_budget_evicts_oldest() -> None:
    one = _frame().nbytes
    cfg = GateConfig(recent_frames=8, recent_spacing_seconds=0.0, recent_max_bytes=2 * one)
    buf = RecentFrames()
    for i in range(6):
        buf.offer(_frame(i), float(i), cfg)
    assert [item.ts for item in buf.latest()] == [4.0, 5.0]


def test_single_frame_over_budget_is_dropped() -> None:
    cfg = GateConfig(recent_max_bytes=10)
    buf = RecentFrames()
    buf.offer(_frame(), 0.0, cfg)
    assert buf.latest() == []


def test_disabled_keeps_nothing() -> None:
    buf = RecentFrames()
    assert buf.offer(_frame(), 0.0, GateConfig(recent_frames=0)) is False
    assert buf.latest() == []


def test_clock_going_backwards_starts_a_new_sequence() -> None:
    buf, cfg = RecentFrames(), GateConfig(recent_spacing_seconds=0.5)
    buf.offer(_frame(), 100.0, cfg)
    buf.offer(_frame(), 101.0, cfg)
    buf.offer(_frame(), 1.0, cfg)
    assert [item.ts for item in buf.latest()] == [1.0]


def test_subsample_caps_width_and_is_read_only_uint8() -> None:
    out = subsample(np.zeros((100, 1000, 3), dtype=np.uint8), 250)
    assert out.shape[1] <= 250 and out.dtype == np.uint8
    with pytest.raises(ValueError):
        out[0, 0, 0] = 1


def test_subsample_converts_other_dtypes() -> None:
    wide = np.full((8, 8), 65535, dtype=np.uint16)
    assert int(subsample(wide, 64).max()) == 255
    floats = np.array([[np.nan, 300.0], [-5.0, 7.5]], dtype=np.float32)
    assert subsample(floats, 64).tolist() == [[0, 255], [0, 7]]


def test_stored_frame_does_not_alias_the_input() -> None:
    gate = ChangeGate(GateConfig(recent_spacing_seconds=0.0), backend="numpy")
    frame = _frame(50)
    gate.process(frame, 0.0)
    frame[:] = 200
    assert int(gate.recent_frames()[0].frame.max()) == 50


def test_gate_exposes_recent_frames_with_defaults() -> None:
    gate = ChangeGate(backend="numpy")
    for i in range(30):
        gate.process(_frame(100 + i % 2), i * 0.1)
    frames = gate.recent_frames()
    assert len(frames) == 3
    assert [round(f.ts, 1) for f in frames] == [1.5, 2.0, 2.5]
    assert [f.ts for f in gate.recent_frames(2)] == [2.0, 2.5]


def test_gate_reset_drops_recent_frames() -> None:
    gate = ChangeGate(backend="numpy")
    gate.process(_frame(), 0.0)
    gate.reset()
    assert gate.recent_frames() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"recent_frames": 17},
        {"recent_frames": -1},
        {"recent_spacing_seconds": -1.0},
        {"recent_max_width": 8},
        {"recent_max_bytes": -1},
    ],
)
def test_recent_config_validation(bad: dict) -> None:
    with pytest.raises(ValueError):
        GateConfig(**bad)
