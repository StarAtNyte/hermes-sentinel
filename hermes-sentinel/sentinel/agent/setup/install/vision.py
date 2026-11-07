"""Remind the user which model judges frames (plugins cannot read model routing)."""

from __future__ import annotations

from ..env import SetupEnv
from ..model import FAIL, WARN, Check

__all__ = ["VISION_YAML", "check_vision"]

VISION_YAML = (
    "auxiliary:\n  sentinel_vision:\n    provider: <your provider>\n"
    "    model: <a vision-capable model>"
)


def check_vision(env: SetupEnv) -> Check:
    """FAIL without ``ctx.llm``; otherwise a WARN that the routing is unverifiable."""
    title = "Vision model"
    if env.has_llm is False:
        return Check(
            "vision",
            title,
            FAIL,
            "This Hermes did not give the plugin an LLM interface (ctx.llm).",
            "Update Hermes to a release that supports plugin LLM calls (>=0.22.5).",
        )
    return Check(
        "vision",
        title,
        WARN,
        "Plugins cannot read model routing, so this is unverified: unless you set "
        "the auxiliary task sentinel_vision, it uses your default model, which must "
        "accept images.",
        VISION_YAML,
    )
