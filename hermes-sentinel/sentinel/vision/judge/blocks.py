"""Input blocks for the host's structured-completion call."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from .parsing import clean
from .schema import MAX_EXAMPLE_BYTES, MAX_EXAMPLES_PER_KIND
from .types import Example

logger = logging.getLogger(__name__)

__all__ = ["example_blocks", "image_block", "text_block"]


def image_block(data: bytes, mime: str, name: str) -> Any:
    """A ``PluginLlmImageInput`` when the host type is importable, else the same-shape dict."""
    try:
        from agent.plugin_llm import PluginLlmImageInput
    except ImportError:
        logger.debug("sentinel: host PluginLlmImageInput unavailable; using a plain image block")
        return {"type": "image", "data": data, "mime_type": mime, "file_name": name}
    return PluginLlmImageInput(data=data, mime_type=mime, file_name=name)


def text_block(text: str) -> dict[str, Any]:
    """A plain text block."""
    return {"type": "text", "text": text}


def _kind_blocks(examples: Sequence[Example], kind: str, answer: str) -> list[Any]:
    blocks: list[Any] = []
    chosen = [e for e in examples if e.kind == kind and e.data][:MAX_EXAMPLES_PER_KIND]
    for i, ex in enumerate(chosen, 1):
        if len(ex.data) > MAX_EXAMPLE_BYTES:
            continue
        note = f" ({clean(ex.note, 120)})" if ex.note else ""
        blocks.append(text_block(f"Reference example where the answer is {answer}{note}:"))
        blocks.append(image_block(bytes(ex.data), ex.mime_type, f"example_{answer.lower()}_{i}"))
    return blocks


def example_blocks(examples: Sequence[Example]) -> list[Any]:
    """Labelled teaching-example blocks: the positive ones, then the negative ones."""
    return [*_kind_blocks(examples, "pos", "YES"), *_kind_blocks(examples, "neg", "NO")]
