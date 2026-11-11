"""Motion input: a short, ordered, downscaled frame sequence sent in one model call."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..imaging import encode_image
from .blocks import image_block, text_block

__all__ = [
    "MAX_SEQUENCE_BYTES",
    "MAX_SEQUENCE_FRAMES",
    "MOTION_INSTRUCTIONS",
    "EncodedFrame",
    "SequenceFrame",
    "encode_sequence",
    "sequence_blocks",
]

MAX_SEQUENCE_FRAMES = 3
#: Total encoded size of all sequence images in one call.
MAX_SEQUENCE_BYTES = 1_200_000
#: Longest side tried per image, largest first, until the sequence fits the byte cap.
SEQUENCE_SIDES = (896, 640, 448, 320)

MOTION_INSTRUCTIONS = """\
You are Sentinel, a careful visual monitor. Answer one yes/no question about what is \
HAPPENING over a short moment, using several frames of the same camera.

Question: {question}

The frames are given in time order, oldest first. Each is labelled with its time relative to \
the newest frame (for example "-1.0 s"); the newest is the CURRENT image. Compare the frames \
to see what moved or changed.

Rules:
- Base the answer on the difference between the frames, as well as on what the CURRENT image \
shows. A single still that looks fine says nothing about motion.
- match = true only when the frames clearly show that the answer is "yes".
- confidence = how sure you are that your answer is right, from 0 to 1. Use a low value when \
the frames are dark, blurred, too similar or ambiguous.
- caption = one short sentence for the user describing what is happening (for example "a \
person walked up to the door and reached for the handle").
- reason = the visible evidence across the frames, one sentence.
Reply with JSON only."""


@dataclass(frozen=True)
class SequenceFrame:
    """One frame of a motion sequence; ``offset`` is seconds relative to the newest (<= 0)."""

    offset: float
    frame: Any


@dataclass(frozen=True)
class EncodedFrame:
    """A sequence frame encoded for the model."""

    offset: float
    data: bytes
    mime: str


def _encode_all(frames: Sequence[SequenceFrame], side: int, quality: int) -> list[EncodedFrame]:
    out = []
    for item in frames:
        data, mime = encode_image(item.frame, max_side=side, quality=quality)
        out.append(EncodedFrame(item.offset, data, mime))
    return out


def encode_sequence(
    frames: Sequence[SequenceFrame],
    *,
    quality: int,
    max_bytes: int = MAX_SEQUENCE_BYTES,
) -> list[EncodedFrame]:
    """Encode the newest ``MAX_SEQUENCE_FRAMES`` frames within ``max_bytes`` in total.

    Shrinks every image step by step; if even the smallest size is too big, the oldest frames
    are dropped (a single frame is the floor). Raises ``ValueError`` for an unusable frame.
    """
    kept = list(frames)[-MAX_SEQUENCE_FRAMES:]
    encoded: list[EncodedFrame] = []
    for side in SEQUENCE_SIDES:
        encoded = _encode_all(kept, side, quality)
        if sum(len(e.data) for e in encoded) <= max_bytes:
            return encoded
    while len(encoded) > 1 and sum(len(e.data) for e in encoded) > max_bytes:
        encoded = encoded[1:]
    return encoded


def _offset_label(offset: float) -> str:
    return "CURRENT, t = 0" if offset >= -1e-6 else f"t = {offset:.1f} s"


def sequence_blocks(encoded: Sequence[EncodedFrame]) -> list[Any]:
    """Labelled input blocks, oldest first; the last frame is the CURRENT image."""
    total = len(encoded)
    blocks: list[Any] = [
        text_block(f"{total} consecutive frames follow, oldest first, newest last (CURRENT).")
    ]
    for index, item in enumerate(encoded, 1):
        blocks.append(text_block(f"Frame {index} of {total} ({_offset_label(item.offset)}):"))
        blocks.append(image_block(item.data, item.mime, f"frame_{index}"))
    return blocks
