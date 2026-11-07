"""JSON-ready summaries of rules and events for tool replies."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ....config import SentinelConfig
from ....rules import RuleSpec
from ....storage import EventRecord

__all__ = ["caps_text", "event_dict", "local_time", "rule_brief"]


def local_time(ts: str) -> str:
    """An ISO timestamp as local ``YYYY-MM-DD HH:MM:SS`` (the input unchanged if unparsable)."""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return str(ts)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def event_dict(event: EventRecord, rule: RuleSpec | None = None) -> dict[str, Any]:
    """An event as a plain mapping, with its rule text and ``MEDIA:`` photo when known."""
    out: dict[str, Any] = {
        "event_id": event.id,
        "rule_id": event.rule_id,
        "time": local_time(event.ts),
        "ts_utc": event.ts,
        "caption": event.caption,
        "confidence": round(event.confidence, 2) if event.confidence is not None else None,
        "source": event.source,
        "feedback": event.feedback,
    }
    if rule is not None:
        out["rule"] = rule.text or rule.question
    if event.snapshot_path:
        out["media"] = f"MEDIA:{event.snapshot_path}"
    return out


def rule_brief(spec: RuleSpec) -> dict[str, Any]:
    """A rule as a plain mapping of its user-visible settings."""
    return {
        "rule_id": spec.id,
        "watching_for": spec.text or spec.question,
        "question": spec.question,
        "mode": spec.mode,
        "hold_seconds": spec.hold_seconds,
        "cooldown_seconds": spec.cooldown_seconds,
        "source": spec.source,
        "roi": list(spec.roi) if spec.roi else None,
        "schedule": spec.schedule.to_dict() if spec.schedule else None,
        "action": spec.action,
        "action_prompt": spec.action_prompt or None,
        "confidence_threshold": round(spec.confidence_threshold, 3),
        "enabled": spec.enabled,
        "recipe": spec.recipe,
        "motion": spec.motion,
        "compiled_by": spec.compiled_by,
    }


def caps_text(cfg: SentinelConfig) -> str:
    """The hourly vision cap in words."""
    hour = cfg.vision_calls_per_hour
    return "no cap" if hour is None else f"{hour} vision checks/hour"
