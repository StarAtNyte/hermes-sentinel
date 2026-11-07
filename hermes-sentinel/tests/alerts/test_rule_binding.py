"""Planning ``/sentinel here`` and binding rules to a route."""

from __future__ import annotations

from types import SimpleNamespace

from harness.plugin_loader import plugin_module

alerts = plugin_module("sentinel.alerts")

ROUTE = {"session_key": "k1", "platform": "telegram", "chat_id": "42"}


def spec(rule_id, **origin):
    return SimpleNamespace(id=rule_id, origin=origin, text=f"text {rule_id}", question="q")


def test_target_id_all_and_unknown():
    rules = [spec("a"), spec("b")]
    assert [r.id for r in alerts.plan_here(rules, "b", "").rules] == ["b"]
    assert len(alerts.plan_here(rules, "all", "").rules) == 2
    assert "No rule 'zz'" in alerts.plan_here(rules, "zz", "").reply
    assert "No rules to move" in alerts.plan_here([], "all", "").reply


def test_empty_target_prefers_this_sessions_rules():
    rules = [spec("a", session_id="s1"), spec("b")]
    assert [r.id for r in alerts.plan_here(rules, "", "s1").rules] == ["a"]


def test_empty_target_binds_the_only_unrouted_rule():
    rules = [spec("a", session_key="k0"), spec("b", session_id="other")]
    plan = alerts.plan_here(rules, "", "s1")
    assert [r.id for r in plan.rules] == ["b"] and not plan.reply


def test_empty_target_lists_candidates_with_commands_when_several():
    plan = alerts.plan_here([spec("a"), spec("b")], "", "s1")
    assert not plan.rules
    assert "/sentinel here a" in plan.reply and "/sentinel here b" in plan.reply


def test_empty_target_with_nothing_to_bind_explains():
    plan = alerts.plan_here([spec("a", session_key="k0")], "", "s1")
    assert not plan.rules and "from now on" in plan.reply


class FakeStore:
    def __init__(self):
        self.saved = []

    def upsert_rule(self, rule):
        self.saved.append(rule)
        return rule


class Rule(SimpleNamespace):
    def with_changes(self, **changes):
        return Rule(**{**vars(self), **changes})


def test_bind_rules_keeps_session_fields_and_updates_engine():
    store, updated = FakeStore(), []
    engine = SimpleNamespace(update_rule=updated.append)
    rule = Rule(id="a", origin={"session_id": "s1", "surface": "telegram", "stale": "x"})
    assert alerts.bind_rules(store, engine, [rule], ROUTE) == ["a"]
    assert store.saved[0].origin == {"session_id": "s1", "surface": "telegram", **ROUTE}
    assert updated == store.saved


def test_adopt_only_this_sessions_unrouted_rules():
    store = FakeStore()
    rules = [
        Rule(id="mine", origin={"session_id": "s1"}),
        Rule(id="routed", origin={"session_id": "s1", "session_key": "k"}),
        Rule(id="theirs", origin={"session_id": "s2"}),
    ]
    assert alerts.adopt_session_rules(store, None, rules, "s1", ROUTE) == ["mine"]
    assert alerts.adopt_session_rules(store, None, rules, "s1", {}) == []
    assert alerts.adopt_session_rules(store, None, rules, "", ROUTE) == []
