"""On-demand frames."""

from __future__ import annotations

import numpy as np
import pytest
from harness.engine_rig import FRAME, StaticSource
from harness.fake_sources import SourceError


def test_grab_frame_prefers_fresh_latest_then_one_off(h):
    h.add()
    h.engine.step_source("cam")
    src = h.sources["cam"]
    opens = src.open_count
    assert h.engine.grab_frame("cam") is FRAME
    assert src.open_count == opens  # reused the capture thread's frame
    h.clock.advance(60)
    assert h.engine.grab_frame("cam") is FRAME
    assert src.open_count == opens + 1  # stale: one-off read
    h.sources["other"] = StaticSource(np.zeros((5, 5, 3), np.uint8))
    assert h.engine.grab_frame("other").shape == (5, 5, 3)


def test_grab_frame_errors(h):
    with pytest.raises(SourceError, match="unknown nowhere"):
        h.engine.grab_frame("nowhere")
    bad = StaticSource()
    bad.read_error = RuntimeError("weird")
    h.sources["bad"] = bad
    with pytest.raises(SourceError, match="weird"):
        h.engine.grab_frame("bad")
    assert bad.close_count == 1


def test_grab_frame_falls_back_to_stale_frame(h):
    h.add()
    h.engine.step_source("cam")
    h.clock.advance(60)
    h.sources["cam"].open_error = SourceError("busy")
    assert h.engine.grab_frame("cam") is FRAME
