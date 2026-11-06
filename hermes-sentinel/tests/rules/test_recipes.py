"""The bundled recipes."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module

recipes = plugin_module("sentinel.rules.recipes")
rules = plugin_module("sentinel.rules")


EXPECTED = {
    "package-at-door",
    "pet-on-couch",
    "print-failure",
    "training-loss-plateau",
    "desk-intruder",
    "stove-unattended",
}


def test_six_bundled_recipes():
    assert set(recipes.RECIPES) == EXPECTED == set(recipes.recipe_names())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_recipe_builds_a_valid_rule(name):
    recipe = recipes.RECIPES[name]
    spec = recipes.build_rule(
        recipe, "camera:0", defaults={"confirm_k": 2, "confirm_n": 3, "confidence_threshold": 0.7}
    )
    assert spec.recipe == name and spec.compiled_by == "manual"
    assert spec.question.endswith("?") and spec.mode in rules.MODES
    assert spec.action == "notify" and spec.text == recipe.description
    if spec.mode != "trigger":
        assert spec.hold_seconds > 0
    assert recipe.to_dict()["name"] == name


def test_recipe_lookup_is_forgiving():
    assert recipes.get_recipe("Package_At Door").name == "package-at-door"
    assert recipes.get_recipe("nope") is None
    assert recipes.get_recipe(None) is None


def test_recipe_overrides():
    recipe = recipes.RECIPES["print-failure"]
    spec = recipes.build_rule(
        recipe,
        "camera:1",
        text="my printer",
        roi=[0.1, 0.1, 0.5, 0.5],
        action_prompt="pause the printer",
        origin={"session_id": "s"},
        defaults={"confidence_threshold": 0.6},
    )
    assert spec.text == "my printer" and spec.roi == (0.1, 0.1, 0.5, 0.5)
    assert spec.action == "agent" and spec.action_prompt == "pause the printer"
    assert spec.origin == {"session_id": "s"}
    assert spec.confidence_threshold == 0.75  # the recipe's floor beats a lower default
    assert recipes.RECIPES["training-loss-plateau"].source_hint == "screen"
    with pytest.raises(rules.RuleValidationError):
        recipes.build_rule(recipe, "camera:1", roi=[0.9, 0.9, 0.5, 0.5])
