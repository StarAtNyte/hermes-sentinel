"""``sentinel_look``: grab a frame now and describe it (one vision check)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ....vision import encode_jpeg
from ..common import (
    SOURCE_PROPERTY,
    ToolContext,
    ToolError,
    ToolSpec,
    err,
    ok,
    schema,
    str_arg,
)

logger = logging.getLogger(__name__)

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_look",
    "Grab a frame from a camera or the screen right now and describe it, or answer a "
    "question about it. Returns a MEDIA: path to the photo. Uses one vision check.",
    {
        "source": SOURCE_PROPERTY,
        "question": {"type": "string", "description": "Optional question about the frame."},
    },
)


def _save_look_snapshot(context: ToolContext, frame: Any) -> str | None:
    """``MEDIA:<path>`` of the saved photo, ``None`` when it cannot be encoded or saved."""
    jpeg = encode_jpeg(frame)
    if jpeg is None:
        return None
    try:
        return f"MEDIA:{context.sentinel.store.save_snapshot(jpeg, rule_id='look')}"
    except Exception as exc:  # noqa: BLE001 - the answer matters more than the photo
        logger.warning("sentinel: could not save look snapshot: %s", exc)
        return None


def run(context: ToolContext, args: dict[str, Any], _kwargs: Mapping[str, Any]) -> str:
    """Describe the current frame of a source, or answer ``question`` about it."""
    sentinel = context.sentinel
    source = context.source_arg(args)
    question = str_arg(args, "question", limit=500)
    budget = sentinel.budget
    frame = sentinel.engine.grab_frame(source)
    if not budget.try_consume(rule_id="look"):
        raise ToolError(budget.status().describe())
    result = sentinel.judge.describe(frame, question or None)
    media = _save_look_snapshot(context, frame)
    if not result.ok:
        return err(f"the vision model could not answer: {result.error}", media=media)
    return ok(source=source, answer=result.answer, caption=result.caption, media=media)


SPEC = ToolSpec("sentinel_look", SCHEMA, "\U0001f50d", run)
