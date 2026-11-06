"""Turns a stream of judge verdicts into fire decisions."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

from ..constants import DEFAULT_STALE_AFTER_SECONDS
from ..spec import RuleSpec
from .evidence import verdict_to_observation
from .types import FireEvent, RuleState, StepResult
from .window import ObservationWindow


class RuleStateMachine:
    """k-of-n confirmation, hold, latch and cooldown for one rule. Pure and clock-injected.

    * A **hit** is a verdict that counts as evidence for the rule: ``match=True`` with
      ``confidence >= threshold`` (``trigger``/``condition``), or ``match=False`` with
      ``confidence >= threshold`` (``absence``). Every other valid verdict is a **miss**.
      ``match=None`` (judge error) or a non-finite confidence is ignored.
    * The window holds the last ``confirm_n`` counted verdicts. Truth is *confirmed* when it
      holds ``>= k`` hits; it is *refuted* when it holds ``>= n - k + 1`` misses (at that
      point ``k`` hits are impossible in the window).
    * ``idle``: a hit opens a candidate episode (``truth_since`` = time of that hit).
    * ``candidate``: refuted -> ``idle``. Confirmed, held for ``effective_hold_seconds``
      and the latest verdict is a hit -> fire, enter ``firing``, start the cooldown.
    * ``firing``: latched (no repeat alerts for the same episode). Refuted -> re-arm into
      ``cooldown`` (if the cooldown is still running) or ``idle``.
    * ``cooldown``: evidence is still recorded but nothing fires until the cooldown ends.
    * Staleness: if no verdict was counted for ``stale_after_seconds``, the episode is
      dropped (window cleared; ``candidate`` -> ``idle``, ``firing`` re-arms). Continuity is
      only claimed across verdicts at most that far apart.
    * Time never goes backwards: a ``now`` earlier than the last one is clamped.

    Thread-safe: an internal lock serialises all calls.
    """

    def __init__(
        self,
        spec: RuleSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        """Create a machine for ``spec``; ``clock`` supplies ``now`` when callers omit it."""
        stale = float(stale_after_seconds)
        if not math.isfinite(stale) or stale <= 0:
            raise ValueError("stale_after_seconds must be a positive number")
        self._clock = clock
        self._stale_after = stale
        self._lock = threading.Lock()  # guards every field below
        self._spec = spec
        self._window = ObservationWindow(spec.confirm_n)
        self._state = RuleState.IDLE
        self._truth_since: float | None = None
        self._cooldown_until: float = -math.inf
        self._last_now: float = -math.inf
        self._last_counted: float | None = None
        self._last_fired: float | None = None
        self._fire_count = 0

    # ---- public API -----------------------------------------------------------------

    @property
    def spec(self) -> RuleSpec:
        """The rule currently being evaluated."""
        return self._spec

    @property
    def state(self) -> RuleState:
        """Current lifecycle state."""
        with self._lock:
            return self._state

    @property
    def fire_count(self) -> int:
        """How many times the rule has fired."""
        with self._lock:
            return self._fire_count

    def observe(
        self, match: bool | None, confidence: float, now: float | None = None
    ) -> StepResult:
        """Feed one judge verdict.

        Returns the resulting state and a :class:`FireEvent` if the rule fired on it.
        """
        with self._lock:
            now = self._tick(now)
            previous = self._state
            self._advance_time(now)
            obs = verdict_to_observation(self._spec, match, confidence, now)
            if obs is None:
                return StepResult(self._state, previous, counted=False)
            self._window.add(obs)
            self._last_counted = now
            fired = self._step(now)
            return StepResult(self._state, previous, counted=True, fired=fired)

    def poll(self, now: float | None = None) -> StepResult:
        """Apply time-based transitions (cooldown end, staleness) without a verdict."""
        with self._lock:
            now = self._tick(now)
            previous = self._state
            self._advance_time(now)
            return StepResult(self._state, previous, counted=False)

    def reset(self) -> None:
        """Forget all evidence and cooldown; back to ``idle``."""
        with self._lock:
            self._reset_locked(keep_cooldown=False)

    def update_spec(self, spec: RuleSpec) -> None:
        """Swap in an edited spec.

        A change of mode, k or n resets the evidence (the running cooldown is kept);
        threshold / hold / cooldown edits apply to future verdicts.
        """
        with self._lock:
            old = self._spec
            self._spec = spec
            if (old.mode, old.confirm_k, old.confirm_n) != (
                spec.mode,
                spec.confirm_k,
                spec.confirm_n,
            ):
                self._reset_locked(keep_cooldown=True)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """JSON-safe view of the current state for status displays."""
        with self._lock:
            now = self._peek(now)
            hits, misses = self._window.counts()
            return {
                "rule_id": self._spec.id,
                "state": self._state.value,
                "window": len(self._window),
                "hits": hits,
                "misses": misses,
                "truth_seconds": (
                    max(0.0, now - self._truth_since) if self._truth_since is not None else None
                ),
                "cooldown_remaining": max(0.0, self._cooldown_until - now)
                if math.isfinite(self._cooldown_until)
                else 0.0,
                "fire_count": self._fire_count,
                "seconds_since_fire": (
                    max(0.0, now - self._last_fired) if self._last_fired is not None else None
                ),
            }

    # ---- internals (lock held) ------------------------------------------------------

    def _peek(self, now: float | None) -> float:
        value = self._clock() if now is None else float(now)
        if not math.isfinite(value):
            raise ValueError("now must be finite")
        return max(value, self._last_now)

    def _tick(self, now: float | None) -> float:
        value = self._peek(now)
        self._last_now = value
        return value

    def _reset_locked(self, *, keep_cooldown: bool) -> None:
        self._window = ObservationWindow(self._spec.confirm_n)
        self._truth_since = None
        self._last_counted = None
        if not keep_cooldown:
            self._cooldown_until = -math.inf
        self._state = (
            RuleState.COOLDOWN
            if keep_cooldown and self._last_now < self._cooldown_until
            else RuleState.IDLE
        )

    def _rearm(self, now: float) -> None:
        self._window.clear()
        self._truth_since = None
        self._state = RuleState.COOLDOWN if now < self._cooldown_until else RuleState.IDLE

    def _advance_time(self, now: float) -> None:
        if self._last_counted is not None and now - self._last_counted > self._stale_after:
            # Lost track: evidence too old to claim continuity.
            self._last_counted = None
            if self._state in (RuleState.CANDIDATE, RuleState.FIRING):
                self._rearm(now)
            else:
                self._window.clear()
        if self._state is RuleState.COOLDOWN and now >= self._cooldown_until:
            self._state = RuleState.IDLE

    def _step(self, now: float) -> FireEvent | None:
        if self._state is RuleState.COOLDOWN:
            return None
        if self._state is RuleState.IDLE and not self._begin_episode():
            return None
        refute_at = self._spec.confirm_n - self._spec.confirm_k + 1
        _, misses = self._window.counts()
        if self._state is RuleState.FIRING:
            if misses >= refute_at:
                self._rearm(now)
            return None
        return self._step_candidate(now, refuted=misses >= refute_at)

    def _begin_episode(self) -> bool:
        """Idle: drop leading misses; if a hit remains, open a candidate episode."""
        self._window.drop_leading_misses()
        if not len(self._window):
            return False
        self._state = RuleState.CANDIDATE
        self._truth_since = self._window.first_ts
        return True

    def _step_candidate(self, now: float, *, refuted: bool) -> FireEvent | None:
        if refuted:
            self._window.clear()
            self._truth_since = None
            self._state = RuleState.IDLE
            return None
        assert self._truth_since is not None
        spec = self._spec
        hits, _ = self._window.counts()
        held = now - self._truth_since
        if (
            hits >= spec.confirm_k
            and self._window.last_is_hit  # never fire on a verdict that just said "no"
            and held >= spec.effective_hold_seconds
            and now >= self._cooldown_until
        ):
            return self._fire(now, hits, held)
        return None

    def _fire(self, now: float, hits: int, held: float) -> FireEvent:
        spec = self._spec
        assert self._truth_since is not None
        confidences = self._window.hit_confidences()
        event = FireEvent(
            rule_id=spec.id,
            mode=spec.mode,
            ts=now,
            confidence=sum(confidences) / len(confidences),
            truth_since=self._truth_since,
            held_seconds=held,
            hits=hits,
            window=len(self._window),
        )
        self._state = RuleState.FIRING
        self._cooldown_until = now + spec.cooldown_seconds
        self._last_fired = now
        self._fire_count += 1
        return event
