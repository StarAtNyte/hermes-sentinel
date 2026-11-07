"""The bounded judge queue."""

from __future__ import annotations

import numpy as np
from harness.engine_rig import FRAME
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")


def test_queue_is_bounded_and_drops_oldest(h):
    for rid in ("a", "b", "c"):
        h.add(rule_id=rid)
    for rid in ("a", "b", "c"):
        h.gates[rid].results.append("change")
    h.engine.step_source("cam")
    assert h.engine.queue_length() == 2
    status = h.engine.status()
    assert status["dropped_jobs"] == 1
    assert h.engine.rule_status("a")["dropped"] == 1
    h.engine.run_pending_judgements()
    assert [c.rule_id for c in h.judge.calls] == ["b", "c"]


def test_one_queued_job_per_rule_newest_frame_wins(h):
    h.add()
    h.gates["r1"].results.extend(["tick", "change"])
    h.engine.step_source("cam")
    newer = np.full_like(FRAME, 7)
    h.sources["cam"].frame = newer
    h.engine.step_source("cam")
    assert h.engine.queue_length() == 1
    job = h.engine.queue.items[0]
    assert job.frame is newer and job.reason == "change"


def test_a_rule_is_never_judged_twice_at_once(h):
    h.add()
    rt = h.engine.state.rules["r1"]
    h.gates["r1"].results.append("change")
    h.engine.step_source("cam")
    rt.in_flight = True
    assert h.engine.run_pending_judgements() == 0
    rt.in_flight = False
    assert h.engine.run_pending_judgements() == 1
