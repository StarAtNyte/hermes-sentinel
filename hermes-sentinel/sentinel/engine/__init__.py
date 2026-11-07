"""The watch engine: capture threads, change gates, judge workers, scheduler and health.

Data flow per source::

    capture thread --frame--> ChangeGate (one per rule, ROI aware)
        --changed / tick / confirm--> bounded judge queue (drop oldest)
            --> judge worker (pool of 2): budget -> Judge -> RuleStateMachine
                --fired--> snapshot + event in the store -> Alerter

Who does what (all share one :class:`~sentinel.engine.state.EngineState`):

* :mod:`capture`: one capture thread per source, shared by every rule watching it. A failing
  source is reopened with exponential backoff (1 s doubling to 60 s); one "offline" notice
  after ``offline_grace`` seconds and one "back online" notice follow.
* :mod:`jobs`: the gate decides per frame whether a rule is judged (change, idle tick, or a
  k-of-n confirmation look). Rules in cooldown are not queued. The queue holds at most one job
  per rule and ``queue_size`` overall, dropping the oldest; a rule is never judged by two
  workers at once.
* :mod:`verdicts`: judge workers batch several rules queued from one frame into one call,
  judge motion rules on their recent frames, re-check borderline hits once on a crop, and
  reserve the vision budget for every call. Judge failures back a rule off (5 s doubling to
  300 s) with one notice per failure episode.
* :class:`Scheduler`: once a second polls state machines, prunes old data and re-syncs rules
  from the store. :class:`Lifecycle`: start, standby and stop; one engine per data directory
  (a lock file), a losing process retries in a standby thread.
* Secret scope: a capture thread runs in a copy of the context of the call that started it,
  and every tool or command call primes an in-memory secret cache. A camera whose
  ``${SECRET}`` cannot be read on a scope-less thread reports "waiting for secret scope"
  and re-checks every few seconds instead of going offline.
* A finite source (a video file) that reaches its end stops with one notice.

Every thread is a daemon and :meth:`Engine.stop` joins them all. ``threaded=False`` builds an
engine that never starts threads; tests drive it with :meth:`Engine.step_source`,
:meth:`Engine.run_pending_judgements` and :meth:`Engine.maintenance`, deterministically.
"""

from .engine import Engine
from .infra import ProcessLock, default_source_key
from .settings import EngineSettings

__all__ = ["Engine", "EngineSettings", "ProcessLock", "default_source_key"]
