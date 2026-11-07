"""Shared fakes for engine tests: stub gate/judge/source and a synchronous Harness engine."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from types import SimpleNamespace

import numpy as np

from harness.fake_sources import FakeClock, SourceError
from harness.plugin_loader import plugin_module

engine_mod = plugin_module("sentinel.engine")
alerts = plugin_module("sentinel.alerts")
budget_mod = plugin_module("sentinel.budget")
config_mod = plugin_module("sentinel.config")
judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")
store_mod = plugin_module("sentinel.storage")
Verdict = judge_mod.Verdict
FRAME = np.full((60, 80, 3), 100, np.uint8)


class StubGate:
    """Reports what the test queues (default: quiet)."""

    def __init__(self, spec):
        self.results = deque()
        self.roi = spec.roi
        self.frames = 0

    def process(self, frame, ts=None):
        self.frames += 1
        if self.results:
            kind = self.results.popleft()
            return SimpleNamespace(changed=kind == "change", tick=kind == "tick")
        return SimpleNamespace(changed=False, tick=False)

    def set_roi(self, roi):
        self.roi = roi


class StubJudge:
    def __init__(self):
        self.verdicts = deque()
        self.calls = []
        self.default = Verdict(False, 0.9, caption="nothing")

    def judge(self, frame, question, *, rule_id, roi=None, examples=()):
        self.calls.append(SimpleNamespace(rule_id=rule_id, roi=roi, examples=list(examples)))
        return self.verdicts.popleft() if self.verdicts else self.default


class StaticSource:
    def __init__(self, frame=FRAME):
        self.frame = frame
        self.open_count = 0
        self.close_count = 0
        self.open_error = None
        self.read_error = None

    def open(self):
        self.open_count += 1
        if self.open_error:
            raise self.open_error

    def read(self):
        if self.read_error:
            raise self.read_error
        return 0.0, self.frame

    def close(self):
        self.close_count += 1


class Harness:
    def __init__(self, ctx, *, per_hour=None, settings=None, config=None, **engine_kw):
        self.ctx = ctx
        self.clock = FakeClock(100.0)
        self.wall = [datetime(2026, 11, 6, 12, 0).timestamp()]
        self.store = store_mod.Store(clock=lambda: self.wall[0])
        self.judge = StubJudge()
        self.gates = {}
        self.sources = {}
        self.alerter = alerts.Alerter(ctx, clock=self.clock, per_rule_min_interval=0)
        self.budget = budget_mod.Budget(
            self.store, per_hour=per_hour, per_day=None, clock=lambda: self.wall[0]
        )
        self.engine = engine_mod.Engine(
            store=self.store,
            judge=self.judge,
            alerter=self.alerter,
            budget=self.budget,
            config=config or config_mod.SentinelConfig(batch_judging=False),
            settings=settings
            or engine_mod.EngineSettings(
                confirm_interval=1.0,
                offline_grace=3.0,
                backoff_initial=1.0,
                backoff_max=8.0,
                judge_backoff_initial=5.0,
                judge_backoff_max=20.0,
                queue_size=2,
            ),
            source_factory=self.factory,
            source_key=lambda s: s,
            gate_factory=self.gate,
            clock=self.clock,
            wall_clock=lambda: self.wall[0],
            threaded=False,
            **engine_kw,
        )

    def factory(self, spec):
        src = self.sources.get(spec)
        if src is None:
            raise SourceError(f"unknown {spec}")
        return src

    def gate(self, spec):
        g = StubGate(spec)
        self.gates[spec.id] = g
        return g

    def add(self, rule_id="r1", source="cam", **kw):
        self.sources.setdefault(source, StaticSource())
        spec = rules.RuleSpec(question=f"{rule_id}?", source=source, id=rule_id, **kw).validate()
        self.store.upsert_rule(spec)
        self.engine.add_rule(spec)
        self.engine.step_source(source)  # creates the gate
        return spec

    def step(self, source="cam", gate_result=None, rule_id="r1"):
        if gate_result:
            self.gates[rule_id].results.append(gate_result)
        out = self.engine.step_source(source)
        self.engine.run_pending_judgements()
        return out

    def notices(self, needle=""):
        return [
            m.content
            for m in self.ctx.injected
            if m.content.startswith("[Sentinel notice]") and needle in m.content
        ]
