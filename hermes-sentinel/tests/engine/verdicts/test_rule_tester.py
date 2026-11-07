"""Judging a rule once on demand."""

from __future__ import annotations

from harness.engine_rig import Verdict


def test_test_rule(h):
    h.add()
    h.judge.verdicts.append(Verdict(True, 0.8, caption="yes it is"))
    out = h.engine.test_rule("r1")
    assert out["ok"] and out["match"] is True and out["would_count"] is True
    assert h.engine.rule_status("r1")["state"] == "idle"  # not fed to the state machine
    assert h.engine.test_rule("missing")["ok"] is False
    h.judge.verdicts.append(Verdict(True, 0.8))
    h.engine.update_rule(h.store.get_rule("r1").with_changes(mode="absence"))
    assert h.engine.test_rule("r1")["would_count"] is False  # absence counts a "no"
    h.engine.update_rule(h.store.get_rule("r1").with_changes(source="gone"))
    assert "could not read" in h.engine.test_rule("r1")["error"]
    h.engine.remove_rule("r1")
    assert h.engine.test_rule("r1")["ok"] is True  # falls back to the store copy (cam)
