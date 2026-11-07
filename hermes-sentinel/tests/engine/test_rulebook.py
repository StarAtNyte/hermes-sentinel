"""Installing, updating, removing and syncing rules."""

from __future__ import annotations

from harness.engine_rig import StaticSource
from harness.plugin_loader import plugin_module

judge_mod = plugin_module("sentinel.vision")
rules = plugin_module("sentinel.rules")


def test_disabled_rule_detaches_and_closes_source(h):
    spec = h.add()
    src = h.sources["cam"]
    h.engine.update_rule(spec.with_changes(enabled=False))
    assert h.engine.source_keys() == []
    assert src.close_count == 1
    assert h.engine.step_source("cam") == "missing"
    h.engine.update_rule(spec.with_changes(enabled=True))
    assert h.engine.source_keys() == ["cam"]


def test_moving_a_rule_to_another_source(h):
    spec = h.add()
    h.sources["cam2"] = StaticSource()
    h.engine.update_rule(spec.with_changes(source="cam2", roi=(0, 0, 0.5, 0.5)))
    assert h.engine.source_keys() == ["cam2"]
    h.engine.step_source("cam2")
    assert h.gates["r1"].roi == (0, 0, 0.5, 0.5)


def test_roi_edit_updates_existing_gate(h):
    spec = h.add()
    gate = h.gates["r1"]
    h.engine.update_rule(spec.with_changes(roi=(0.5, 0.5, 0.5, 0.5)))
    assert h.gates["r1"] is gate and gate.roi == (0.5, 0.5, 0.5, 0.5)


def test_remove_rule(h):
    h.add()
    h.gates["r1"].results.append("change")
    h.engine.step_source("cam")
    assert h.engine.remove_rule("r1") is True
    assert h.engine.queue_length() == 0 and h.engine.source_keys() == []
    assert h.engine.remove_rule("r1") is False


def test_sync_from_store_adds_updates_and_removes(h):
    h.sources["cam"] = StaticSource()
    spec = rules.RuleSpec(question="q?", source="cam", id="ext").validate()
    h.store.upsert_rule(spec)
    h.engine.sync_from_store()
    assert h.engine.rule_ids() == ["ext"]
    h.store.upsert_rule(spec.with_changes(question="new?"))
    h.engine.sync_from_store()
    assert h.engine.state.rules["ext"].spec.question == "new?"
    h.store.delete_rule("ext")
    h.engine.maintenance()  # sync is due at start
    assert h.engine.rule_ids() == []
