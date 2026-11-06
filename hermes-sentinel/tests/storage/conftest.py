"""Fixtures shared by the storage tests."""

from __future__ import annotations

import pytest
from harness.store_helpers import Clock, store_mod


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock)
    yield s
    s.close()


@pytest.fixture
def like_store(plugin_data_dir, clock):
    s = store_mod.Store(clock=clock, enable_fts=False, db_filename="like.db")
    yield s
    s.close()
