"""Check the vision call caps for values that stop or over-spend judging."""

from __future__ import annotations

from ..env import SetupEnv
from ..model import FAIL, OK, WARN, Check

__all__ = ["check_budget"]

HOURLY_SANE_MAX = 600


def check_budget(env: SetupEnv) -> Check:
    """FAIL for a cap of 0, WARN for no cap, a daily cap below the hourly one, or a huge cap."""
    title = "Vision call caps"
    cfg = env.config
    hour = getattr(cfg, "vision_calls_per_hour", None)
    day = getattr(cfg, "vision_calls_per_day", None)
    if hour == 0 or day == 0:
        return Check(
            "budget",
            title,
            FAIL,
            "A cap of 0 pauses all judging; nothing can fire.",
            "Set vision_calls_per_hour / vision_calls_per_day above 0.",
        )
    if hour is None or day is None:
        return Check(
            "budget",
            title,
            WARN,
            "No cap on vision calls: a busy scene can cost a lot.",
            "Set vision_calls_per_hour (default 60) and vision_calls_per_day (500).",
        )
    if day < hour:
        return Check(
            "budget",
            title,
            WARN,
            f"Daily cap {day} is below the hourly cap {hour}; the daily cap will bind.",
            "Raise vision_calls_per_day or lower vision_calls_per_hour.",
        )
    if hour > HOURLY_SANE_MAX:
        return Check(
            "budget",
            title,
            WARN,
            f"{hour}/hour is a high cap for a cloud model.",
            "Lower vision_calls_per_hour unless a local model does the judging.",
        )
    return Check("budget", title, OK, f"{hour}/hour, {day}/day.")
