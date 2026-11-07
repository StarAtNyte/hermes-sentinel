"""Judge failures: per-rule backoff and one notice per failure episode."""

from __future__ import annotations

from typing import Any

from ...vision import Verdict
from ..infra import short_error
from ..models import RuleRuntime
from ..state import EngineState

__all__ = ["JudgeHealth"]

_NOTICE_SENT = 0
_SUCCESS_SINCE = 1


class JudgeHealth:
    """Backs a rule off after a failed judgement and rate-limits the "checks failing" notice."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (settings, alerter, last error)."""
        self._state = state
        # Per source key: [notice sent, time of first success since the last failure]. No lock:
        # each access is a single dict operation and entries are independent per source.
        self.errors: dict[str, list[Any]] = {}

    def failed(self, rt: RuleRuntime, verdict: Verdict, now: float) -> None:
        """Count a failed judgement, back the rule off and post the notice once per episode."""
        state = self._state
        settings = state.settings
        rt.errors += 1
        rt.consecutive_errors += 1
        backoff = min(
            settings.judge_backoff_max,
            settings.judge_backoff_initial * (2 ** min(rt.consecutive_errors - 1, 30)),
        )
        rt.judge_backoff_until = now + backoff
        state.last_error = short_error(verdict.error or "judge failed")
        episode = self.errors.setdefault(rt.source_key, [False, None])
        episode[_SUCCESS_SINCE] = None  # a failure ends the success streak
        if not episode[_NOTICE_SENT]:
            episode[_NOTICE_SENT] = True
            state.alerter.notice(
                f"Vision checks are failing ({state.last_error}). The camera keeps being "
                "watched locally and checks resume automatically.",
                origin=rt.spec.origin,
            )

    def succeeded(self, source_key: str, now: float) -> None:
        """Re-arm the judge-error notice after ``judge_error_rearm`` seconds of steady success.

        This keeps a flaky model from sending a notice per failure episode.
        """
        episode = self.errors.get(source_key)
        if episode is None or not episode[_NOTICE_SENT]:
            return
        if episode[_SUCCESS_SINCE] is None:
            episode[_SUCCESS_SINCE] = now
        elif now - episode[_SUCCESS_SINCE] >= self._state.settings.judge_error_rearm:
            del self.errors[source_key]
