"""The ``motion`` rule flag: default off, keyword heuristic, compiler field, recipes."""

from __future__ import annotations

import pytest
from harness.fake_ctx import FakeLlm
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")
heuristic = plugin_module("sentinel.rules.compiler.motion")


@pytest.mark.parametrize(
    "text",
    [
        "Is someone walking toward the front door?",
        "Is the water level rising in the tank?",
        "Is the garage door closing?",
        "Is a car pulling away from the curb?",
        "Is the dog running across the lawn?",
    ],
)
def test_motion_questions_are_detected(text):
    assert heuristic.looks_like_motion(text)


@pytest.mark.parametrize(
    "text",
    [
        "Is there a package on the doorstep?",
        "Is the red mug gone from the desk?",
        "Is a person sitting at the desk?",
        "Is the stove on?",
    ],
)
def test_static_questions_stay_off(text):
    assert not heuristic.looks_like_motion(text)


def test_non_text_is_ignored():
    assert heuristic.looks_like_motion(None, 3) is False  # type: ignore[arg-type]


def test_spec_defaults_off_and_round_trips():
    spec = rules.RuleSpec(question="Is it there?", source="camera:0")
    assert spec.motion is False
    on = spec.with_changes(motion=True)
    assert rules.RuleSpec.from_json(on.to_json()).motion is True
    legacy = {k: v for k, v in on.to_dict().items() if k != "motion"}
    assert rules.RuleSpec.from_dict(legacy).motion is False


def test_spec_rejects_a_non_boolean_motion():
    with pytest.raises(rules.RuleValidationError):
        rules.RuleSpec(question="q?", source="s", motion="yes").validate()  # type: ignore[arg-type]


def test_fallback_compiler_uses_the_heuristic():
    moving = rules.compile_rule("tell me if someone is walking toward the door", "camera:0", None)
    still = rules.compile_rule("tell me if there is a package at the door", "camera:0", None)
    assert moving.motion is True and still.motion is False


def test_llm_flag_is_honoured_and_the_heuristic_still_applies():
    llm = FakeLlm(plugin_id="p", owned_tasks=lambda: frozenset({rules.RULES_TASK}))
    llm.script({"question": "Is the gate swinging?", "mode": "trigger", "motion": None})
    llm.script({"question": "Is the shelf tidy?", "mode": "trigger", "motion": True})
    llm.script({"question": "Is the shelf tidy?", "mode": "trigger", "motion": False})
    assert rules.compile_rule("x", "s", llm).motion is True  # heuristic: "swinging"
    assert rules.compile_rule("x", "s", llm).motion is True  # the model said so
    assert rules.compile_rule("x", "s", llm).motion is False


def test_recipes_flag_and_heuristic():
    recipe = rules.Recipe(
        name="x", title="x", description="d", question="Is the shelf tidy?", mode="trigger"
    )
    assert rules.build_rule(recipe, "camera:0").motion is False
    flagged = rules.Recipe(
        name="y",
        title="y",
        description="d",
        question="Is the shelf tidy?",
        mode="trigger",
        motion=True,
    )
    assert rules.build_rule(flagged, "camera:0").motion is True
    assert not any(rules.build_rule(r, "camera:0").motion for r in rules.RECIPES.values())
