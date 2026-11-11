"""``sentinel_feedback``: mark an alert correct or a false alarm and retune the rule."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....rules import RuleSpec, lowered_threshold, raised_threshold
from ....storage import EventRecord, StoreError
from ..common import ToolContext, ToolError, ToolSpec, int_arg, ok, schema, str_arg
from .examples import example_from_event

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_feedback",
    "Mark an alert as correct or a false alarm. A false alarm becomes a negative example "
    "and makes the rule slightly stricter; a correct one becomes a positive example.",
    {
        "event_id": {"type": "integer", "description": "Event number from the alert."},
        "verdict": {"type": "string", "enum": ["correct", "false_alarm"]},
        "note": {"type": "string", "description": "Optional note, e.g. what it really was."},
    },
    ["event_id", "verdict"],
)


def _verdict_arg(args: Mapping[str, Any]) -> str:
    verdict = str_arg(args, "verdict", required=True, limit=16).lower().replace(" ", "_")
    if verdict in ("false", "wrong", "falsealarm"):
        verdict = "false_alarm"
    if verdict not in ("correct", "false_alarm"):
        raise ToolError("verdict must be 'correct' or 'false_alarm'")
    return verdict


def _retune(context: ToolContext, spec: RuleSpec, threshold: float) -> RuleSpec:
    """Save ``spec`` with a new confidence threshold when it differs."""
    if threshold == spec.confidence_threshold:
        return spec
    return context.save_and_apply(spec.with_changes(confidence_threshold=threshold))


def _already_recorded(context: ToolContext, event: EventRecord, verdict: str) -> str:
    """Idempotent reply: no second example, no second nudge."""
    current = context.sentinel.store.get_rule(event.rule_id)
    return ok(
        event_id=event.id,
        feedback=verdict,
        already_recorded=True,
        rule_id=event.rule_id,
        **({"confidence_threshold": current.confidence_threshold} if current else {}),
        message=f"Event #{event.id} was already marked as {verdict.replace('_', ' ')}; "
        "nothing changed.",
    )


def _false_alarm_message(event: EventRecord, spec: RuleSpec) -> str:
    return (
        f"Marked event #{event.id} as a false alarm. Saved it as a 'no' example "
        f"and raised the rule's confidence threshold to "
        f"{spec.confidence_threshold:.2f}."
    )


def _correct_message(event: EventRecord, spec: RuleSpec, before: float) -> str:
    message = f"Marked event #{event.id} as correct and saved it as a 'yes' example."
    if spec.confidence_threshold < before:
        message += (
            f" Nudged the rule's confidence threshold down to {spec.confidence_threshold:.2f}."
        )
    return message


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Record the verdict on an event, save its photo as an example and nudge the threshold."""
    event_id = int_arg(args, "event_id", required=True)
    verdict = _verdict_arg(args)
    note = str_arg(args, "note", limit=500)
    store = context.sentinel.store
    event = store.get_event(int(event_id))  # type: ignore[arg-type]
    if event is None or not context.owns_event(event, context.caller(kwargs)):
        raise ToolError(f"no event #{event_id}; use sentinel_events to find it")
    if event.feedback == verdict:
        return _already_recorded(context, event, verdict)
    store.set_feedback(event.id, verdict)
    result: dict[str, Any] = {"event_id": event.id, "feedback": verdict}
    spec = store.get_rule(event.rule_id)
    if spec is None:
        return ok(**result, message="Feedback saved (the rule no longer exists).")
    if event.snapshot_path:
        try:
            kind = "neg" if verdict == "false_alarm" else "pos"
            result["example_id"] = example_from_event(context, spec.id, kind, event, note).id
        except (StoreError, ValueError, OSError) as exc:
            result["example_error"] = str(exc)[:200]
    before = spec.confidence_threshold
    if verdict == "false_alarm":
        spec = _retune(context, spec, raised_threshold(before))
        message = _false_alarm_message(event, spec)
    else:
        spec = _retune(context, spec, lowered_threshold(before))
        message = _correct_message(event, spec, before)
    result["confidence_threshold"] = spec.confidence_threshold
    return ok(**result, rule_id=spec.id, message=message)


SPEC = ToolSpec("sentinel_feedback", SCHEMA, "\U0001f44d", run)
