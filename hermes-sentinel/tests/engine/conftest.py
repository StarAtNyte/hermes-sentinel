"""Fixtures for the engine tests."""

from __future__ import annotations

import pytest
from harness.engine_rig import Harness


@pytest.fixture
def h(fake_ctx):
    harness = Harness(fake_ctx)
    yield harness
    harness.engine.stop()
    harness.store.close()
