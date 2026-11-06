"""Shared helpers for the vision judge tests."""

from __future__ import annotations

import numpy as np

from harness.fake_ctx import FakeLlm, PluginLlmStructuredResult
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision.judge")
TASK = judge_mod.VISION_TASK


def frame(w=320, h=240, value=128):
    img = np.full((h, w, 3), value, dtype=np.uint8)
    img[h // 4 : h // 2, w // 4 : w // 2] = (40, 40, 200)
    return img


def llm_with(*responses):
    llm = FakeLlm(plugin_id="hermes-sentinel", owned_tasks=lambda: frozenset({TASK}))
    llm.script(*responses)
    return llm


def structured(text="", parsed=None, model="m"):
    return PluginLlmStructuredResult(
        text=text, provider="p", model=model, agent_id="a", parsed=parsed
    )
