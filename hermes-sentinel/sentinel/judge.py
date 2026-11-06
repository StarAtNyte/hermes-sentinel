"""Vision judge: asks the host's vision model a yes/no question about a frame.

One ``ctx.llm.complete_structured`` call per judgement, on the plugin's own auxiliary task
``sentinel_vision`` (``VISION_TASK``) so users can route it to any vision-capable model:

* the current frame, JPEG-encoded with its longest side at most ``max_side`` (1024) pixels;
* a close-up crop of the rule's ROI (fractions ``x, y, w, h``) when the ROI is a small part of
  the frame;
* up to two positive and two negative teaching examples (bytes from the event store), each
  preceded by a text block saying which answer it shows.

The reply must match :data:`VERDICT_SCHEMA`. Parsing is defensive (fences, strings for
booleans, percentages, missing fields) and every failure - trust refusal, timeout, bad JSON,
an exception inside the host - becomes ``Verdict(match=None, error=...)``. Nothing here
raises to the caller.

cv2 is used for resizing and JPEG encoding when available. Without it, frames are
area-downscaled with numpy and sent as PNG (zlib), so the judge still works.
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DESCRIBE_SCHEMA",
    "VERDICT_SCHEMA",
    "VISION_TASK",
    "VISION_TASK_INFO",
    "Description",
    "Example",
    "Judge",
    "Verdict",
    "crop_roi",
    "encode_image",
    "encode_jpeg",
]

VISION_TASK = "sentinel_vision"
#: Keyword arguments for ``ctx.register_auxiliary_task(VISION_TASK, **VISION_TASK_INFO)``.
VISION_TASK_INFO: dict[str, Any] = {
    "display_name": "Sentinel vision judge",
    "description": "Looks at camera or screen frames and answers a watch rule's yes/no "
    "question (needs a vision-capable model).",
}

DEFAULT_MAX_SIDE = 1024
DEFAULT_JPEG_QUALITY = 85
DEFAULT_TIMEOUT = 60.0
MAX_EXAMPLES_PER_KIND = 2
MAX_EXAMPLE_BYTES = 4 * 1024 * 1024
MAX_CAPTION_LEN = 300
MAX_REASON_LEN = 600
MAX_ANSWER_LEN = 1500
MAX_ERROR_LEN = 300
#: Only add the ROI close-up when the ROI covers less than this fraction of the frame.
ROI_CROP_MAX_AREA = 0.6
MIN_CROP_SIDE = 16

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean", "description": "Answer to the question: true = yes."},
        "confidence": {
            "type": "number",
            "description": "Probability (0..1) that the answer is right.",
        },
        "reason": {"type": "string", "description": "Visible evidence, one sentence."},
        "caption": {
            "type": "string",
            "description": "One short sentence describing what is happening, for the user.",
        },
    },
    "required": ["match", "confidence"],
}

DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Answer to the question, or a description."},
        "caption": {"type": "string", "description": "One short sentence summary."},
    },
    "required": ["answer"],
}

_JUDGE_INSTRUCTIONS = """\
You are Sentinel, a careful visual monitor. Answer one yes/no question about the CURRENT image.

Question: {question}

Rules:
- Judge only what is visible in the CURRENT image (and its close-up, if given). Labelled \
reference images are examples from the past, not the current scene.
- match = true only when the answer to the question is clearly "yes".
- confidence = how sure you are that your answer is right, from 0 to 1. Use a low value when \
the view is dark, blurred, blocked or ambiguous.
- caption = one short sentence for the user describing what is happening that is relevant \
to the question (for example "a person in a grey hoodie picked up the mug").
- reason = the visible evidence, one sentence.
Reply with JSON only."""

_DESCRIBE_INSTRUCTIONS = """\
You are Sentinel, looking at a live camera or screen image for the user.
{task}
Be concrete and brief. Do not guess about things you cannot see.
Reply with JSON only: "answer" (the answer or description) and "caption" (one short sentence)."""

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "match", "present"})
_FALSE_WORDS = frozenset({"false", "no", "n", "0", "absent", "none"})


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Verdict:
    """One judgement. ``match`` is ``None`` when the judge failed (see ``error``)."""

    match: bool | None
    confidence: float = 0.0
    reason: str = ""
    caption: str = ""
    error: str | None = None
    elapsed: float = 0.0
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and self.match is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": self.match,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "caption": self.caption,
            "error": self.error,
            "elapsed": round(self.elapsed, 3),
            "model": self.model,
        }


@dataclass(frozen=True)
class Description:
    """Result of :meth:`Judge.describe` (a free-form look at a frame)."""

    answer: str = ""
    caption: str = ""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Example:
    """A teaching example image: ``kind`` is ``"pos"`` (answer yes) or ``"neg"`` (answer no)."""

    kind: str
    data: bytes
    mime_type: str = "image/jpeg"
    note: str = ""


# --------------------------------------------------------------------------- image helpers


def _np() -> Any:
    import numpy as np  # local: the plugin must load without numpy

    return np


def _cv2() -> Any | None:
    try:
        import cv2
    except Exception:  # noqa: BLE001 - ImportError or a broken binary wheel
        return None
    return cv2


def _as_bgr_uint8(frame: Any) -> Any:
    """HxW / HxWx1 / HxWx3 / HxWx4 array of any numeric dtype -> contiguous HxWx3 uint8."""
    np = _np()
    arr = np.asarray(frame)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[..., 0]
    if arr.ndim == 2:
        arr = np.repeat(arr[..., None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] not in (3, 4) or arr.shape[0] < 1 or arr.shape[1] < 1:
        raise ValueError(f"unsupported frame shape {getattr(arr, 'shape', None)}")
    arr = arr[..., :3]
    if arr.dtype != np.uint8:
        data = np.nan_to_num(arr.astype(np.float64), nan=0.0, posinf=255.0, neginf=0.0)
        if arr.dtype.kind == "f" and data.size and float(data.max()) <= 1.0:
            data = data * 255.0
        elif arr.dtype.kind in "ui" and arr.dtype.itemsize > 1:
            data = data / 257.0
        arr = np.clip(np.rint(data), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def _resize_max_side(img: Any, max_side: int) -> Any:
    """Downscale so the longest side is at most ``max_side`` (never upscales)."""
    np = _np()
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    cv2 = _cv2()
    if cv2 is not None:
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    # numpy fallback: integer block mean, then nearest-neighbour trim to the target size.
    factor = math.ceil(longest / max_side)
    hh, ww = (h // factor) * factor, (w // factor) * factor
    small = img[:hh, :ww].reshape(hh // factor, factor, ww // factor, factor, 3).mean(axis=(1, 3))
    return np.ascontiguousarray(np.clip(np.rint(small), 0, 255).astype(np.uint8))


def _png_bytes(img: Any) -> bytes:
    """Minimal RGB PNG encoder (zlib) used when cv2 is not installed."""
    np = _np()
    rgb = img[..., ::-1]  # BGR -> RGB
    h, w = rgb.shape[:2]
    raw = np.empty((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 0] = 0  # filter type "None" per scanline
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def encode_image(
    frame: Any, *, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_JPEG_QUALITY
) -> tuple[bytes, str]:
    """Encode a frame for the model: ``(bytes, mime_type)``.

    JPEG via cv2 when available, otherwise PNG. The longest side is at most ``max_side``.
    Raises ``ValueError`` for an unusable frame.
    """
    img = _resize_max_side(_as_bgr_uint8(frame), max(16, int(max_side)))
    cv2 = _cv2()
    if cv2 is not None:
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return bytes(buf.tobytes()), "image/jpeg"
    return _png_bytes(img), "image/png"


def encode_jpeg(
    frame: Any, *, max_side: int = DEFAULT_MAX_SIDE, quality: int = DEFAULT_JPEG_QUALITY
) -> bytes | None:
    """JPEG bytes for storage (snapshots, examples), or ``None`` when cv2 is unavailable or
    the frame is unusable. Never raises."""
    try:
        data, mime = encode_image(frame, max_side=max_side, quality=quality)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentinel: could not encode frame: %s", exc)
        return None
    return data if mime == "image/jpeg" else None


def crop_roi(frame: Any, roi: Sequence[float] | None) -> Any | None:
    """Crop ``roi`` (fractions ``x, y, w, h``) out of ``frame``; ``None`` when there is no ROI,
    it covers most of the frame, or the crop would be tiny."""
    if roi is None:
        return None
    try:
        x, y, w, h = (float(v) for v in roi)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    if w * h >= ROI_CROP_MAX_AREA:
        return None
    img = _as_bgr_uint8(frame)
    fh, fw = img.shape[:2]
    x0, y0 = max(0, int(math.floor(x * fw))), max(0, int(math.floor(y * fh)))
    x1, y1 = min(fw, int(math.ceil((x + w) * fw))), min(fh, int(math.ceil((y + h) * fh)))
    if x1 - x0 < MIN_CROP_SIDE or y1 - y0 < MIN_CROP_SIDE:
        return None
    return img[y0:y1, x0:x1]


# --------------------------------------------------------------------------- parsing


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL_RE.sub(" ", value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().strip(".!").lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _parse_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        percent = value.strip().endswith("%")
        try:
            number = float(text)
        except ValueError:
            return {"high": 0.9, "medium": 0.6, "low": 0.3}.get(text.lower())
        if percent:
            number /= 100.0
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if 1.0 < number <= 100.0:  # a percentage without the sign
        number /= 100.0
    return min(1.0, number)


def _extract_object(result: Any) -> dict[str, Any] | None:
    """The JSON object from a structured result (``parsed``, else the raw text)."""
    parsed = getattr(result, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(result, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    match = _JSON_OBJ_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_verdict(result: Any, *, elapsed: float = 0.0) -> Verdict:
    """Turn a host result into a :class:`Verdict` (never raises)."""
    model = _clean(getattr(result, "model", ""), 120)
    obj = _extract_object(result)
    if obj is None:
        return Verdict(None, error="model reply was not a JSON object", elapsed=elapsed,
                       model=model)
    match = _parse_bool(obj.get("match", obj.get("answer")))
    if match is None:
        return Verdict(None, error="model reply has no yes/no 'match'", elapsed=elapsed,
                       model=model)
    confidence = _parse_confidence(obj.get("confidence"))
    if confidence is None:
        return Verdict(None, error="model reply has no usable 'confidence'", elapsed=elapsed,
                       model=model)
    return Verdict(
        match=match,
        confidence=confidence,
        reason=_clean(obj.get("reason"), MAX_REASON_LEN),
        caption=_clean(obj.get("caption"), MAX_CAPTION_LEN),
        elapsed=elapsed,
        model=model,
    )


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        kind = "not permitted"
    elif isinstance(exc, TimeoutError):
        kind = "timed out"
    else:
        kind = type(exc).__name__
    detail = _clean(str(exc), MAX_ERROR_LEN)
    return f"{kind}: {detail}" if detail else kind


# --------------------------------------------------------------------------- judge


def _image_block(data: bytes, mime: str, name: str) -> Any:
    """A ``PluginLlmImageInput`` when the host type is importable, else the same-shape dict."""
    try:
        from agent.plugin_llm import PluginLlmImageInput
    except Exception:  # noqa: BLE001 - outside Hermes
        return {"type": "image", "data": data, "mime_type": mime, "file_name": name}
    return PluginLlmImageInput(data=data, mime_type=mime, file_name=name)


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class Judge:
    """Builds vision requests and parses verdicts. Thread-safe (no mutable state).

    ``llm`` is ``ctx.llm`` or a zero-argument callable returning it (looked up per call, so a
    host that swaps the object is fine).
    """

    def __init__(
        self,
        llm: Any,
        *,
        task: str | None = VISION_TASK,
        timeout: float = DEFAULT_TIMEOUT,
        max_side: int = DEFAULT_MAX_SIDE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self.task = task
        self.timeout = float(timeout)
        self.max_side = int(max_side)
        self.jpeg_quality = int(jpeg_quality)
        self._clock = clock

    def _client(self) -> Any:
        llm = self._llm
        if callable(llm) and not hasattr(llm, "complete_structured"):
            llm = llm()
        if llm is None or not hasattr(llm, "complete_structured"):
            raise RuntimeError("no LLM access available (ctx.llm missing)")
        return llm

    def build_input(
        self,
        frame: Any,
        *,
        roi: Sequence[float] | None = None,
        examples: Sequence[Example] = (),
    ) -> list[Any]:
        """The input blocks for one judgement. Raises ``ValueError`` for a bad frame."""
        blocks: list[Any] = []
        pos = [e for e in examples if e.kind == "pos" and e.data][:MAX_EXAMPLES_PER_KIND]
        neg = [e for e in examples if e.kind == "neg" and e.data][:MAX_EXAMPLES_PER_KIND]
        for i, ex in enumerate(pos, 1):
            if len(ex.data) > MAX_EXAMPLE_BYTES:
                continue
            note = f" ({_clean(ex.note, 120)})" if ex.note else ""
            blocks.append(_text_block(f"Reference example where the answer is YES{note}:"))
            blocks.append(_image_block(bytes(ex.data), ex.mime_type, f"example_yes_{i}"))
        for i, ex in enumerate(neg, 1):
            if len(ex.data) > MAX_EXAMPLE_BYTES:
                continue
            note = f" ({_clean(ex.note, 120)})" if ex.note else ""
            blocks.append(_text_block(f"Reference example where the answer is NO{note}:"))
            blocks.append(_image_block(bytes(ex.data), ex.mime_type, f"example_no_{i}"))
        data, mime = encode_image(frame, max_side=self.max_side, quality=self.jpeg_quality)
        blocks.append(_text_block("CURRENT image:"))
        blocks.append(_image_block(data, mime, "current"))
        crop = crop_roi(frame, roi)
        if crop is not None:
            cdata, cmime = encode_image(crop, max_side=self.max_side, quality=self.jpeg_quality)
            blocks.append(_text_block("Close-up of the watched region of the CURRENT image:"))
            blocks.append(_image_block(cdata, cmime, "current_roi"))
        return blocks

    def judge(
        self,
        frame: Any,
        question: str,
        *,
        rule_id: str,
        roi: Sequence[float] | None = None,
        examples: Sequence[Example] = (),
    ) -> Verdict:
        """Ask ``question`` about ``frame``. Never raises; failures carry ``error``."""
        start = self._clock()

        def elapsed() -> float:
            return max(0.0, self._clock() - start)

        if not isinstance(question, str) or not question.strip():
            return Verdict(None, error="rule has no question")
        try:
            blocks = self.build_input(frame, roi=roi, examples=examples)
        except Exception as exc:  # noqa: BLE001
            return Verdict(None, error=f"bad frame: {_error_text(exc)}")
        try:
            result = self._client().complete_structured(
                instructions=_JUDGE_INSTRUCTIONS.format(question=_clean(question, 500)),
                input=blocks,
                json_schema=VERDICT_SCHEMA,
                schema_name="sentinel_verdict",
                temperature=0.0,
                max_tokens=300,
                timeout=self.timeout,
                purpose=f"sentinel:{rule_id}",
                task=self.task,
            )
        except Exception as exc:  # noqa: BLE001 - the judge never raises
            logger.info("sentinel: judge call failed for %s: %s", rule_id, _error_text(exc))
            return Verdict(None, error=_error_text(exc), elapsed=elapsed())
        try:
            return parse_verdict(result, elapsed=elapsed())
        except Exception as exc:  # noqa: BLE001 - defensive: parsing must never raise
            return Verdict(None, error=f"unparseable reply: {_error_text(exc)}", elapsed=elapsed())

    def describe(
        self, frame: Any, question: str | None = None, *, purpose: str = "sentinel:look"
    ) -> Description:
        """Describe ``frame`` or answer a free-form ``question`` about it. Never raises."""
        start = self._clock()
        task = (
            f"Answer this question about the image: {_clean(question, 500)}"
            if question and question.strip()
            else "Describe what the image shows, focusing on people, objects and anything "
            "unusual."
        )
        try:
            data, mime = encode_image(frame, max_side=self.max_side, quality=self.jpeg_quality)
        except Exception as exc:  # noqa: BLE001
            return Description(error=f"bad frame: {_error_text(exc)}")
        try:
            result = self._client().complete_structured(
                instructions=_DESCRIBE_INSTRUCTIONS.format(task=task),
                input=[_text_block("Current image:"), _image_block(data, mime, "current")],
                json_schema=DESCRIBE_SCHEMA,
                schema_name="sentinel_look",
                temperature=0.2,
                max_tokens=500,
                timeout=self.timeout,
                purpose=purpose,
                task=self.task,
            )
        except Exception as exc:  # noqa: BLE001
            return Description(error=_error_text(exc), elapsed=max(0.0, self._clock() - start))
        elapsed = max(0.0, self._clock() - start)
        obj = _extract_object(result)
        if obj is None:
            text = _clean(getattr(result, "text", ""), MAX_ANSWER_LEN)
            if text:
                return Description(answer=text, caption=text[:MAX_CAPTION_LEN], elapsed=elapsed)
            return Description(error="model reply was empty", elapsed=elapsed)
        answer = _clean(obj.get("answer"), MAX_ANSWER_LEN)
        caption = _clean(obj.get("caption"), MAX_CAPTION_LEN) or answer[:MAX_CAPTION_LEN]
        if not answer:
            return Description(error="model reply has no answer", elapsed=elapsed)
        return Description(answer=answer, caption=caption, elapsed=elapsed)
