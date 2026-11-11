"""Mutable runtime records the engine keeps per rule, per judge job and per source."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..judging import JudgeItem
from ..rules import RuleSpec, RuleStateMachine
from ..vision import Verdict

if TYPE_CHECKING:  # runtime import would be circular: jobs.gating imports this module
    from .jobs.pacing import RulePacing

__all__ = ["Job", "Prepared", "RuleRuntime", "SourceWorker"]


@dataclass(eq=False)
class Job:
    """One queued judge request for a rule."""

    rule_id: str
    ts: float
    frame: Any
    reason: str
    bbox: tuple[int, int, int, int] | None = None  # gate's changed region, input-frame pixels
    sequence: tuple[Any, ...] = ()  # motion rules: earlier frames plus this one, oldest first
    confirming: bool = False  # an extra look of a candidate episode, spaced by confirm_spacing


@dataclass(eq=False)
class RuleRuntime:
    """Everything the engine tracks for one rule: spec, state machine, gate and counters."""

    spec: RuleSpec
    machine: RuleStateMachine
    source_key: str
    pacing: RulePacing
    gate: Any = None
    queued: Job | None = None
    in_flight: bool = False
    checks: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    budget_skips: int = 0
    dropped: int = 0
    last_verdict: Verdict | None = None
    last_judged_at: float | None = None  # wall clock
    last_reason: str = ""
    next_confirm_at: float = 0.0
    deferred: bool = False  # a change or tick was held back by pacing; look again when allowed
    judge_backoff_until: float = 0.0
    last_event_id: int | None = None


@dataclass(eq=False)
class Prepared:
    """A dequeued job that passed the pre-judge checks, with its rule and judge input."""

    job: Job
    rt: RuleRuntime
    item: JudgeItem


@dataclass(eq=False)
class SourceWorker:
    """One capture thread's state: the shared source, its health and the newest frame."""

    key: str
    spec: str
    rule_ids: set[str] = field(default_factory=set)
    source: Any = None
    is_open: bool = False
    online: bool | None = None  # None: never connected yet
    failures: int = 0
    next_attempt: float = 0.0
    offline_since: float | None = None
    offline_notified: bool = False
    last_error: str = ""
    latest: tuple[float, Any] | None = None
    frames: int = 0
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    # Guards is_open/source/rule_ids swaps only; never held across open()/read()/close().
    lock: threading.RLock = field(default_factory=threading.RLock)
    latest_lock: threading.Lock = field(default_factory=threading.Lock)  # guards latest
