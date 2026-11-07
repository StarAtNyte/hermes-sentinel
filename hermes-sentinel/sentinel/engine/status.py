"""Read-only status views of the engine for tools, commands and the dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .jobs import JobQueue
from .state import EngineState

__all__ = ["StatusReporter"]


class StatusReporter:
    """Builds the JSON-safe status dicts; pure in-memory reads, no device I/O."""

    def __init__(self, state: EngineState, queue: JobQueue) -> None:
        """Share the engine ``state``; queue depth and drops come from ``queue``."""
        self._state = state
        self._queue = queue

    def rule_status(self, rule_id: str) -> dict[str, Any] | None:
        """Live status of one rule (state machine snapshot plus judge counters), or ``None``."""
        state = self._state
        with state.lock:
            rt = state.rules.get(rule_id)
            if rt is None:
                return None
            worker = state.workers.get(rt.source_key)
            online = worker.online if worker is not None else None
            source_error = worker.last_error if worker is not None else ""
        verdict = rt.last_verdict
        return {
            **rt.machine.snapshot(),
            "source_key": rt.source_key,
            "source_online": online,
            "source_error": source_error,
            "checks": rt.checks,
            "errors": rt.errors,
            "budget_skips": rt.budget_skips,
            "dropped": rt.dropped,
            "last_verdict": verdict.to_dict() if verdict is not None else None,
            "last_checked": (
                datetime.fromtimestamp(rt.last_judged_at).astimezone().isoformat(timespec="seconds")
                if rt.last_judged_at
                else None
            ),
            "last_reason": rt.last_reason,
            "judging_paused_for": max(0.0, rt.judge_backoff_until - state.clock()),
        }

    def status(self) -> dict[str, Any]:
        """Engine-wide status: flags, sources, queue depth, counters and the last error."""
        state = self._state
        with state.lock:
            sources = [
                {
                    "source": w.key,
                    "online": w.online,
                    "error": w.last_error,
                    "frames": w.frames,
                    "rules": sorted(w.rule_ids),
                    "retry_in": max(0.0, w.next_attempt - state.clock()) if not w.is_open else 0,
                }
                for w in state.workers.values()
            ]
            rules = len(state.rules)
            active = sum(1 for r in state.rules.values() if r.spec.enabled)
        return {
            "running": state.running,
            "standing_by": state.standing_by,
            "rules": rules,
            "active_rules": active,
            "sources": sorted(sources, key=lambda s: s["source"]),
            "queue": len(self._queue),
            "dropped_jobs": self._queue.dropped,
            "events_fired": state.fired,
            "alerts": state.alerter.stats(),
            "last_error": state.last_error,
        }
