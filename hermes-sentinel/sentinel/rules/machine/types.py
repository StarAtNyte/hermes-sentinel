"""Value types of the rule state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuleState(StrEnum):
    """Lifecycle of one rule's evidence."""

    IDLE = "idle"  # armed; waiting for a positive verdict
    CANDIDATE = "candidate"  # positive evidence seen; waiting for k-of-n and hold
    FIRING = "firing"  # fired; latched until the question is confirmed false
    COOLDOWN = "cooldown"  # re-armed but inside the cooldown window; cannot fire yet


@dataclass(frozen=True)
class Observation:
    """One counted verdict: when it arrived, whether it was evidence, and its confidence."""

    ts: float
    hit: bool
    confidence: float


@dataclass(frozen=True)
class FireEvent:
    """Emitted once when a rule fires."""

    rule_id: str
    mode: str
    ts: float
    confidence: float
    truth_since: float
    held_seconds: float
    hits: int
    window: int


@dataclass(frozen=True)
class StepResult:
    """Outcome of one ``RuleStateMachine.observe`` / ``poll`` call."""

    state: RuleState
    previous: RuleState
    counted: bool
    fired: FireEvent | None = None

    @property
    def changed(self) -> bool:
        """True if the state differs from before the call."""
        return self.state is not self.previous
