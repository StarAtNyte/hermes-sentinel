"""A synchronous engine with the real Judge on a scripted FakeLlm and stub gates.

Rules on one source are staged with ``stage(...)`` (what each gate reports for the next frame),
then one capture + judging round runs with ``round()``. ``answers`` maps a rule id to the
``(match, confidence)`` the fake model gives it, whether asked alone or in a batch.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import numpy as np

from harness.fake_ctx import FakeLlm
from harness.fake_sources import FakeClock
from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_pkg = plugin_module("sentinel.vision.judge")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.storage")

TASK = judge_pkg.VISION_TASK
FRAME = np.random.default_rng(3).integers(0, 255, (240, 320, 3), dtype=np.uint8)
_ID_RE = re.compile(r'- id "([^"]+)"')


class StubGate:
    """Reports what a test staged; keeps the recent frames a motion rule asks for."""

    def __init__(self, spec):
        self.staged = deque()
        self.recent = []

    def process(self, frame, ts=None):
        kind, bbox = self.staged.popleft() if self.staged else ("quiet", None)
        return SimpleNamespace(changed=kind == "change", tick=kind == "tick", bbox=bbox)

    def recent_frames(self, count=None):
        return list(self.recent)

    def set_roi(self, roi):
        pass


class StaticSource:
    """Returns the very same frame object every read, like one capture shared by all rules."""

    def __init__(self, frame=FRAME):
        self.frame = frame

    def open(self):
        pass

    def read(self):
        return 0.0, self.frame

    def close(self):
        pass


def vision_reply(match: bool, confidence: float) -> dict:
    """A single-rule verdict reply."""
    return {"match": match, "confidence": confidence, "reason": "r", "caption": "c"}


class JudgingRig:
    """Engine + real Judge + FakeLlm. ``answers`` default to a confident ``no``."""

    def __init__(self, ctx, *, per_hour=None, queue_size=16, **config):
        self.ctx = ctx
        self.clock = FakeClock(100.0)
        self.wall = [datetime(2026, 11, 7, 12, 0).timestamp()]
        self.store = store_mod.Store(clock=lambda: self.wall[0])
        self.llm = FakeLlm(plugin_id="hermes-sentinel", owned_tasks=lambda: frozenset({TASK}))
        self.answers: dict[str, tuple[bool, float]] = {}
        self.verify_answer: tuple[bool, float] | Exception = (True, 0.9)
        self.batch_reply = None
        self.llm.respond(self._respond, task=TASK)
        self.gates: dict[str, StubGate] = {}
        self.source = StaticSource()
        self.budget = budget_mod.Budget(
            self.store, per_hour=per_hour, per_day=None, clock=lambda: self.wall[0]
        )
        self.engine = engine_mod.Engine(
            store=self.store,
            judge=judge_pkg.Judge(self.llm),
            alerter=alerts.Alerter(ctx, clock=self.clock, per_rule_min_interval=0),
            budget=self.budget,
            config=config_mod.SentinelConfig(**config),
            settings=engine_mod.EngineSettings(confirm_interval=1.0, queue_size=queue_size),
            source_factory=lambda spec: self.source,
            source_key=lambda s: s,
            gate_factory=self._gate,
            clock=self.clock,
            wall_clock=lambda: self.wall[0],
            threaded=False,
        )

    def _gate(self, spec):
        gate = StubGate(spec)
        self.gates[spec.id] = gate
        return gate

    def _respond(self, call):
        purpose = call.purpose or ""
        if purpose.startswith("sentinel:verify:"):
            if isinstance(self.verify_answer, Exception):
                raise self.verify_answer
            return vision_reply(*self.verify_answer)
        if purpose == "sentinel:batch":
            if self.batch_reply is not None:
                if isinstance(self.batch_reply, Exception):
                    raise self.batch_reply
                return self.batch_reply
            ids = _ID_RE.findall(call.kwargs["instructions"])
            return {"verdicts": [{"rule_id": i, **vision_reply(*self._answer(i))} for i in ids]}
        return vision_reply(*self._answer(purpose.split(":", 1)[1]))

    def _answer(self, rule_id):
        return self.answers.get(rule_id, (False, 0.9))

    def add(self, rule_id, **kw):
        kw.setdefault("confirm_k", 1)
        kw.setdefault("confirm_n", 1)
        spec = rules.RuleSpec(question=f"{rule_id}?", source="cam", id=rule_id, **kw).validate()
        self.store.upsert_rule(spec)
        self.engine.add_rule(spec)
        self.engine.step_source("cam")  # creates the gate
        return spec

    def stage(self, *rule_ids, kind="change", bbox=None):
        for rule_id in rule_ids:
            self.gates[rule_id].staged.append((kind, bbox))

    def round(self):
        self.engine.step_source("cam")
        return self.engine.run_pending_judgements()

    @property
    def vision_calls(self):
        return self.llm.calls_for(task=TASK)

    def purposes(self):
        return [c.purpose for c in self.vision_calls]

    def used(self):
        return self.store.usage_totals()[0]

    def alert_texts(self):
        return [m.content for m in self.ctx.injected if m.content.startswith("[Sentinel alert]")]

    def close(self):
        self.engine.stop()
        self.store.close()
