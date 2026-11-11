"""Field-for-field copies of the public ``agent/plugin_llm.py`` data classes and error.

The ``agent.plugin_llm`` shim module (:mod:`harness.hermes_shims`) exports exactly these
objects and :class:`harness.fake_ctx.FakeLlm` raises / returns them, so a plugin's
``except PluginLlmTrustError`` or ``isinstance(x, PluginLlmImageInput)`` sees the same class
the fake uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PluginLlmCompleteResult",
    "PluginLlmImageInput",
    "PluginLlmInput",
    "PluginLlmStructuredResult",
    "PluginLlmTextInput",
    "PluginLlmTrustError",
    "PluginLlmUsage",
]


@dataclass
class PluginLlmTextInput:
    """Text block in a structured input list."""

    text: str
    type: str = "text"


@dataclass
class PluginLlmImageInput:
    """Image block: ``data`` (raw bytes) or ``url`` (http(s)/data: URL)."""

    data: bytes | None = None
    url: str | None = None
    mime_type: str = "image/png"
    file_name: str = ""
    type: str = "image"


PluginLlmInput = PluginLlmTextInput | PluginLlmImageInput | dict[str, Any]


@dataclass
class PluginLlmUsage:
    """Token + cost usage; every field optional."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class PluginLlmCompleteResult:
    """Result of ``PluginLlm.complete``."""

    text: str
    provider: str
    model: str
    agent_id: str
    usage: PluginLlmUsage = field(default_factory=PluginLlmUsage)
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginLlmStructuredResult:
    """Result of ``PluginLlm.complete_structured``; ``parsed`` only for valid JSON output."""

    text: str
    provider: str
    model: str
    agent_id: str
    usage: PluginLlmUsage = field(default_factory=PluginLlmUsage)
    parsed: Any | None = None
    content_type: str = "text"
    audit: dict[str, Any] = field(default_factory=dict)


class PluginLlmTrustError(PermissionError):
    """Raised when a plugin attempts an LLM override (or task route) without trust."""
