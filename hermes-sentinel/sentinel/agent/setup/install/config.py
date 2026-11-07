"""Surface problems found while reading the plugin settings."""

from __future__ import annotations

from ..env import SetupEnv
from ..masking import mask
from ..model import WARN, Check

__all__ = ["check_config"]


def check_config(env: SetupEnv) -> Check | None:
    """WARN listing the settings problems, or ``None`` when the settings are clean."""
    problems = list(getattr(env.config, "problems", ()) or ())
    if not problems:
        return None
    return Check(
        "config",
        "Plugin settings",
        WARN,
        mask("; ".join(problems)),
        "Fix the listed values under plugins.entries.hermes-sentinel.settings.",
    )
