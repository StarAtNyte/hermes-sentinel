"""The vision judge: builds a request, calls the host model, parses the verdict."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from ..imaging import DEFAULT_JPEG_QUALITY, DEFAULT_MAX_SIDE, crop_roi, encode_image
from .batch import (
    BATCH_INSTRUCTIONS,
    BATCH_SCHEMA,
    BatchOutcome,
    BatchQuestion,
    batch_outcome,
    batch_questions_text,
    failed_batch,
)
from .blocks import example_blocks, image_block, text_block
from .motion import MOTION_INSTRUCTIONS, SequenceFrame, encode_sequence, sequence_blocks
from .parsing import clean, error_text, extract_object, parse_verdict
from .schema import (
    DEFAULT_TIMEOUT,
    DESCRIBE_INSTRUCTIONS,
    DESCRIBE_SCHEMA,
    JUDGE_INSTRUCTIONS,
    MAX_ANSWER_LEN,
    MAX_CAPTION_LEN,
    VERDICT_SCHEMA,
    VISION_TASK,
)
from .types import Description, Example, Verdict
from .verify import VERIFY_INSTRUCTIONS, VERIFY_JPEG_QUALITY, VERIFY_MAX_SIDE

__all__ = ["Judge"]

logger = logging.getLogger(__name__)


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
        sequence: Sequence[SequenceFrame] = (),
    ) -> list[Any]:
        """The input blocks for one judgement. Raises ``ValueError`` for a bad frame.

        With a ``sequence`` of at least two frames (oldest first, the last one is ``frame``'s
        moment) the images are sent in time order instead of one still.
        """
        blocks = example_blocks(examples)
        if len(sequence) >= 2:
            encoded = encode_sequence(sequence, quality=self.jpeg_quality)
            blocks.extend(sequence_blocks(encoded))
        else:
            data, mime = encode_image(frame, max_side=self.max_side, quality=self.jpeg_quality)
            blocks.append(text_block("CURRENT image:"))
            blocks.append(image_block(data, mime, "current"))
        crop = crop_roi(frame, roi)
        if crop is not None:
            cdata, cmime = encode_image(crop, max_side=self.max_side, quality=self.jpeg_quality)
            blocks.append(text_block("Close-up of the watched region of the CURRENT image:"))
            blocks.append(image_block(cdata, cmime, "current_roi"))
        return blocks

    def judge(
        self,
        frame: Any,
        question: str,
        *,
        rule_id: str,
        roi: Sequence[float] | None = None,
        examples: Sequence[Example] = (),
        sequence: Sequence[SequenceFrame] = (),
    ) -> Verdict:
        """Ask ``question`` about ``frame`` (or a motion ``sequence``). Never raises.

        Failures come back as a verdict whose ``error`` is set.
        """
        if not isinstance(question, str) or not question.strip():
            return Verdict(None, error="rule has no question")
        start = self._clock()
        template = MOTION_INSTRUCTIONS if len(sequence) >= 2 else JUDGE_INSTRUCTIONS
        try:
            blocks = self.build_input(frame, roi=roi, examples=examples, sequence=sequence)
        except Exception as exc:  # noqa: BLE001 - broad: encoder errors surface as Verdict.error
            return Verdict(None, error=f"bad frame: {error_text(exc)}")
        return self._ask(template.format(question=clean(question, 500)), blocks, rule_id, start)

    def _ask(self, instructions: str, blocks: list[Any], rule_id: str, start: float) -> Verdict:
        """Send one verdict request and parse the reply; failures become an error verdict."""

        def elapsed() -> float:
            return max(0.0, self._clock() - start)

        try:
            result = self._client().complete_structured(
                instructions=instructions,
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
        except Exception as exc:  # noqa: BLE001 - broad: reply shape is untrusted, surfaced as Verdict.error
            logger.info("sentinel: unparseable judge reply: %s", error_text(exc))
            return Verdict(None, error=f"unparseable reply: {error_text(exc)}", elapsed=elapsed())

    def judge_batch(self, frame: Any, questions: Sequence[BatchQuestion]) -> BatchOutcome:
        """Answer several rules' questions about one ``frame`` in a single model call.

        Never raises. A transport or model failure sets ``call_failed``; a reply that cannot be
        used (wholly or for some rules) lists those rules in ``missing`` so the caller can judge
        them one by one.
        """
        start = self._clock()
        ids = [q.rule_id for q in questions]
        try:
            data, mime = encode_image(frame, max_side=self.max_side, quality=self.jpeg_quality)
        except Exception as exc:  # noqa: BLE001 - broad: encoder errors surface as BatchOutcome.error
            return BatchOutcome(
                missing=tuple(ids), error=f"bad frame: {error_text(exc)}", call_failed=True
            )
        blocks = [text_block("CURRENT image:"), image_block(data, mime, "current")]
        try:
            result = self._client().complete_structured(
                instructions=BATCH_INSTRUCTIONS.format(questions=batch_questions_text(questions)),
                input=blocks,
                json_schema=BATCH_SCHEMA,
                schema_name="sentinel_batch",
                temperature=0.0,
                max_tokens=150 + 150 * len(ids),
                timeout=self.timeout,
                purpose="sentinel:batch",
                task=self.task,
            )
        except Exception as exc:  # noqa: BLE001 - the judge never raises
            logger.info("sentinel: batch judge call failed: %s", error_text(exc))
            return failed_batch(exc, ids, max(0.0, self._clock() - start))
        return batch_outcome(result, ids, max(0.0, self._clock() - start))

    def verify(self, crop: Any, question: str, *, rule_id: str) -> Verdict:
        """A second, differently worded look at a higher-resolution ``crop``. Never raises."""
        start = self._clock()

        def elapsed() -> float:
            return max(0.0, self._clock() - start)

        try:
            data, mime = encode_image(crop, max_side=VERIFY_MAX_SIDE, quality=VERIFY_JPEG_QUALITY)
        except Exception as exc:  # noqa: BLE001 - broad: encoder errors surface as Verdict.error
            return Verdict(None, error=f"bad frame: {error_text(exc)}")
        blocks = [
            text_block("Close-up, higher resolution, of the part of the scene that changed:"),
            image_block(data, mime, "closeup"),
        ]
        try:
            result = self._client().complete_structured(
                instructions=VERIFY_INSTRUCTIONS.format(question=clean(question, 500)),
                input=blocks,
                json_schema=VERDICT_SCHEMA,
                schema_name="sentinel_verification",
                temperature=0.0,
                max_tokens=300,
                timeout=self.timeout,
                purpose=f"sentinel:verify:{rule_id}",
                task=self.task,
            )
        except Exception as exc:  # noqa: BLE001 - broad: third-party LLM client, surfaced as Verdict.error
            logger.info("sentinel: second opinion failed for %s: %s", rule_id, error_text(exc))
            return Verdict(None, error=error_text(exc), elapsed=elapsed())
        try:
            return parse_verdict(result, elapsed=elapsed())
        except Exception as exc:  # noqa: BLE001 - broad: reply shape is untrusted, surfaced as Verdict.error
            logger.info("sentinel: unparseable judge reply: %s", error_text(exc))
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
        except Exception as exc:  # noqa: BLE001 - broad: encoder errors surface as Description.error
            return Description(error=f"bad frame: {error_text(exc)}")
        try:
            result = self._client().complete_structured(
                instructions=DESCRIBE_INSTRUCTIONS.format(task=task),
                input=[text_block("Current image:"), image_block(data, mime, "current")],
                json_schema=DESCRIBE_SCHEMA,
                schema_name="sentinel_look",
                temperature=0.2,
                max_tokens=500,
                timeout=self.timeout,
                purpose=purpose,
                task=self.task,
            )
        except Exception as exc:  # noqa: BLE001 - broad: third-party LLM client, surfaced as Description.error
            logger.info("sentinel: describe call failed: %s", error_text(exc))
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
