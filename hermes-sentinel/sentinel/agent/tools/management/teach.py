"""``sentinel_teach``: add a positive or negative example image to a rule."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ....rules import RuleSpec
from ....vision import encode_jpeg
from ..common import (
    EXAMPLE_JPEG_QUALITY,
    EXAMPLE_MAX_SIDE,
    ToolContext,
    ToolError,
    ToolSpec,
    decode_to_jpeg,
    int_arg,
    ok,
    schema,
    str_arg,
)
from .examples import example_from_event

__all__ = ["MAX_IMAGE_FILE_BYTES", "SPEC"]

MAX_IMAGE_FILE_BYTES = 20 * 1024 * 1024
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
_KINDS = {
    "yes": "pos",
    "pos": "pos",
    "positive": "pos",
    "no": "neg",
    "neg": "neg",
    "negative": "neg",
}

SCHEMA = schema(
    "sentinel_teach",
    "Teach a rule with an example image: kind 'yes' shows what should trigger it, 'no' shows "
    "what should not. Give image_path, event_id, or from_source=true (current frame).",
    {
        "rule_id": {"type": "string"},
        "kind": {"type": "string", "enum": ["yes", "no"]},
        "image_path": {"type": "string", "description": "Path to a JPEG/PNG image file."},
        "event_id": {"type": "integer", "description": "Use this event's photo."},
        "from_source": {
            "type": "boolean",
            "description": "Use the rule's source's current frame.",
        },
        "note": {"type": "string"},
    },
    ["rule_id", "kind"],
)


def _kind_arg(args: Mapping[str, Any]) -> str:
    raw_kind = str_arg(args, "kind", required=True, limit=16).lower()
    if raw_kind not in _KINDS:
        raise ToolError("kind must be 'yes' (should trigger) or 'no' (should not)")
    return _KINDS[raw_kind]


def _read_image_file(image_path: str) -> bytes:
    path = Path(image_path).expanduser()
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ToolError("image_path must be a .jpg, .jpeg, .png, .bmp or .webp file")
    try:
        if not path.is_file():
            raise ToolError(f"no image file at {image_path}")
        if path.stat().st_size > MAX_IMAGE_FILE_BYTES:
            raise ToolError("the image is larger than 20 MB")
        return path.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read {image_path}: {exc.strerror or exc}") from None


def _example_from_event_id(
    context: ToolContext,
    spec: RuleSpec,
    kind: str,
    event_id: int,
    note: str,
    kwargs: Mapping[str, Any],
) -> Any:
    event = context.sentinel.store.get_event(event_id)
    if event is None or not context.owns_event(event, context.caller(kwargs)):
        raise ToolError(f"no event #{event_id}")
    return example_from_event(context, spec.id, kind, event, note)


def _example_from_source(context: ToolContext, spec: RuleSpec, kind: str, note: str) -> Any:
    frame = context.sentinel.engine.grab_frame(spec.source)
    jpeg = encode_jpeg(frame, max_side=EXAMPLE_MAX_SIDE, quality=EXAMPLE_JPEG_QUALITY)
    if jpeg is None:
        raise ToolError("could not encode the current frame (is OpenCV installed?)")
    return context.sentinel.store.add_example(spec.id, kind, jpeg, note=note)


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Store one example image (from a file, an event or the live source) for a rule."""
    rule_id = str_arg(args, "rule_id", required=True, limit=64)
    kind = _kind_arg(args)
    note = str_arg(args, "note", limit=500)
    spec = context.rule(rule_id, context.caller(kwargs))
    store = context.sentinel.store
    image_path = str_arg(args, "image_path", limit=4096)
    event_id = int_arg(args, "event_id")
    from_source = bool(args.get("from_source", False))
    given = sum(bool(x) for x in (image_path, event_id is not None, from_source))
    if given != 1:
        raise ToolError("give exactly one of image_path, event_id or from_source=true")
    if event_id is not None:
        example = _example_from_event_id(context, spec, kind, event_id, note, kwargs)
    elif from_source:
        example = _example_from_source(context, spec, kind, note)
    else:
        data = _read_image_file(image_path)
        example = store.add_example(spec.id, kind, decode_to_jpeg(data), note=note)
    counts = {k: len(store.list_examples(spec.id, k)) for k in ("pos", "neg")}
    word = "yes" if kind == "pos" else "no"
    return ok(
        example_id=example.id,
        rule_id=spec.id,
        kind=word,
        examples={"yes": counts["pos"], "no": counts["neg"]},
        message=f"Added a '{word}' example to rule {spec.id}. The two newest of each kind "
        "are shown to the vision model with every check.",
    )


SPEC = ToolSpec("sentinel_teach", SCHEMA, "\U0001f393", run)
