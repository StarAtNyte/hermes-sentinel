"""The vision judge: builds a request, calls the host model, parses the verdict."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from ..imaging import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_SIDE, crop_roi, encode_image
from .parsing import clean, error_text, extract_object, parse_verdict
from .schema import (
    DEFAULT_TIMEOUT,
    DESCRIBE_INSTRUCTIONS,
    DESCRIBE_SCHEMA,
    JUDGE_INSTRUCTIONS,
    MAX_ANSWER_LEN,
    MAX_CAPTION_LEN,
    MAX_EXAMPLE_BYTES,
    MAX_EXAMPLES_PER_KIND,
    VERDICT_SCHEMA,
    VISION_TASK,
)
from .types import Description, Example, Verdict

__all__ = ["Judge"]

logger = logging.getLogger(__name__)


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
        """Configure the judge; ``task`` routes the call to the plugin auxiliary task."""
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
            note = f" ({clean(ex.note, 120)})" if ex.note else ""
            blocks.append(_text_block(f"Reference example where the answer is YES{note}:"))
            blocks.append(_image_block(bytes(ex.data), ex.mime_type, f"example_yes_{i}"))
        for i, ex in enumerate(neg, 1):
            if len(ex.data) > MAX_EXAMPLE_BYTES:
                continue
            note = f" ({clean(ex.note, 120)})" if ex.note else ""
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
            return Verdict(None, error=f"bad frame: {error_text(exc)}")
        try:
            result = self._client().complete_structured(
                instructions=JUDGE_INSTRUCTIONS.format(question=clean(question, 500)),
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
            logger.info("sentinel: judge call failed for %s: %s", rule_id, error_text(exc))
            return Verdict(None, error=error_text(exc), elapsed=elapsed())
        try:
            return parse_verdict(result, elapsed=elapsed())
        except Exception as exc:  # noqa: BLE001 - defensive: parsing must never raise
            return Verdict(None, error=f"unparseable reply: {error_text(exc)}", elapsed=elapsed())

    def describe(
        self, frame: Any, question: str | None = None, *, purpose: str = "sentinel:look"
    ) -> Description:
        """Describe ``frame`` or answer a free-form ``question`` about it. Never raises."""
        start = self._clock()
        task = (
            f"Answer this question about the image: {clean(question, 500)}"
            if question and question.strip()
            else "Describe what the image shows, focusing on people, objects and anything unusual."
        )
        try:
            data, mime = encode_image(frame, max_side=self.max_side, quality=self.jpeg_quality)
        except Exception as exc:  # noqa: BLE001
            return Description(error=f"bad frame: {error_text(exc)}")
        try:
            result = self._client().complete_structured(
                instructions=DESCRIBE_INSTRUCTIONS.format(task=task),
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
            return Description(error=error_text(exc), elapsed=max(0.0, self._clock() - start))
        return self._describe_reply(result, max(0.0, self._clock() - start))

    @staticmethod
    def _describe_reply(result: Any, elapsed: float) -> Description:
        obj = extract_object(result)
        if obj is None:
            text = clean(getattr(result, "text", ""), MAX_ANSWER_LEN)
            if text:
                return Description(answer=text, caption=text[:MAX_CAPTION_LEN], elapsed=elapsed)
            return Description(error="model reply was empty", elapsed=elapsed)
        answer = clean(obj.get("answer"), MAX_ANSWER_LEN)
        caption = clean(obj.get("caption"), MAX_CAPTION_LEN) or answer[:MAX_CAPTION_LEN]
        if not answer:
            return Description(error="model reply has no answer", elapsed=elapsed)
        return Description(answer=answer, caption=caption, elapsed=elapsed)
