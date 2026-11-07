"""Report whether the watcher is running, standing by or not started yet."""

from __future__ import annotations

from ..env import SetupEnv
from ..model import OK, Check

__all__ = ["check_engine"]


def check_engine(env: SetupEnv) -> Check:
    """Always OK: the watcher starts lazily, the detail says which state it is in."""
    title = "Watcher"
    engine = env.engine
    if engine is not None and getattr(engine, "running", False):
        return Check("engine", title, OK, "Running.")
    if engine is not None and getattr(engine, "standing_by", False):
        return Check("engine", title, OK, "Standing by: another Hermes process is watching.")
    return Check(
        "engine",
        title,
        OK,
        "Not started yet; it starts with the first watch rule (or shortly after Hermes "
        "loads when enabled rules exist).",
    )
