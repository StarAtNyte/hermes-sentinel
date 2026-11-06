"""Settings parsing (config_schema keys) and the bundled recipes."""

from __future__ import annotations

import pytest
import yaml
from harness.plugin_loader import PLUGIN_DIR, plugin_module

config = plugin_module("sentinel.config")
recipes = plugin_module("sentinel.recipes")
rules = plugin_module("sentinel.rules")
SentinelConfig = config.SentinelConfig


def load(values):
    return SentinelConfig.load(lambda key, default=None: values.get(key, default))


def test_config_keys_match_manifest_schema():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    schema = manifest["config_schema"]
    assert set(schema) == set(config.CONFIG_KEYS)
    defaults = SentinelConfig()
    for key, spec in schema.items():
        assert spec.get("description"), key
        assert spec["default"] == getattr(defaults, key), key


def test_defaults_without_host():
    cfg = SentinelConfig.load(None)
    assert cfg == SentinelConfig()
    assert cfg.problems == ()
    assert cfg.rule_defaults() == {"confirm_k": 2, "confirm_n": 3, "confidence_threshold": 0.7}
    assert cfg.stale_after_seconds == 900.0


def test_valid_values():
    cfg = load({
        "default_source": " screen:1 ", "vision_calls_per_hour": "30", "vision_calls_per_day": 100,
        "confirm_k": 3, "confirm_n": 5, "confidence_threshold": 0.8, "idle_check_seconds": 600,
        "retention_days": 2, "max_storage_mb": 50.5, "alert_snapshots": "no",
        "stream_urls": {"front-door": "rtsp://cam/${CAM_PW}"},
    })
    assert cfg.problems == ()
    assert cfg.default_source == "screen:1"
    assert (cfg.vision_calls_per_hour, cfg.vision_calls_per_day) == (30, 100)
    assert (cfg.confirm_k, cfg.confirm_n, cfg.confidence_threshold) == (3, 5, 0.8)
    assert cfg.alert_snapshots is False and cfg.max_storage_mb == 50.5
    assert cfg.stream_urls == {"front-door": "rtsp://cam/${CAM_PW}"}
    assert cfg.stale_after_seconds == 1800.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("vision_calls_per_hour", -5), ("vision_calls_per_hour", 1.5),
        ("vision_calls_per_hour", "lots"), ("vision_calls_per_hour", [1]),
        ("confirm_k", 0), ("confirm_n", 99), ("confidence_threshold", 1.5),
        ("confidence_threshold", float("nan")), ("idle_check_seconds", -1),
        ("retention_days", "x"), ("alert_snapshots", "maybe"), ("default_source", ""),
        ("default_source", True), ("stream_urls", ["rtsp://x"]), ("max_storage_mb", True),
    ],
)
def test_bad_values_fall_back_and_are_reported(key, value):
    cfg = load({key: value})
    assert getattr(cfg, key) == getattr(SentinelConfig(), key)
    assert cfg.problems and cfg.problems[0].startswith(key)


def test_k_larger_than_n_resets_to_two_of_three():
    cfg = load({"confirm_k": 4, "confirm_n": 3})
    assert (cfg.confirm_k, cfg.confirm_n) == (2, 3) and cfg.problems


@pytest.mark.parametrize("value", [None, "unlimited", "none", -1])
def test_unlimited_caps(value):
    cfg = load({"vision_calls_per_day": value})
    expected = 500 if value is None else None
    assert cfg.vision_calls_per_day == expected


def test_stream_url_validation():
    cfg = load({"stream_urls": {"ok": "rtsp://a", "bad name": "rtsp://b", "": "x", "n": 5,
                                "long": "x" * 5000, "empty": " "}})
    assert cfg.stream_urls == {"ok": "rtsp://a"}
    assert len(cfg.problems) == 5
    many = {f"s{i}": "rtsp://x" for i in range(100)}
    assert len(load({"stream_urls": many}).stream_urls) == config.MAX_STREAMS


def test_broken_getter_never_raises():
    def get(key, default=None):
        raise KeyError(key)

    cfg = SentinelConfig.load(get)
    assert cfg.vision_calls_per_hour == 60 and len(cfg.problems) == len(config.CONFIG_KEYS)


# --------------------------------------------------------------------------- recipes


EXPECTED = {"package-at-door", "pet-on-couch", "print-failure", "training-loss-plateau",
            "desk-intruder", "stove-unattended"}


def test_six_bundled_recipes():
    assert set(recipes.RECIPES) == EXPECTED == set(recipes.recipe_names())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_recipe_builds_a_valid_rule(name):
    recipe = recipes.RECIPES[name]
    spec = recipes.build_rule(recipe, "camera:0", defaults={"confirm_k": 2, "confirm_n": 3,
                                                            "confidence_threshold": 0.7})
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
    spec = recipes.build_rule(recipe, "camera:1", text="my printer", roi=[0.1, 0.1, 0.5, 0.5],
                              action_prompt="pause the printer", origin={"session_id": "s"},
                              defaults={"confidence_threshold": 0.6})
    assert spec.text == "my printer" and spec.roi == (0.1, 0.1, 0.5, 0.5)
    assert spec.action == "agent" and spec.action_prompt == "pause the printer"
    assert spec.origin == {"session_id": "s"}
    assert spec.confidence_threshold == 0.75  # the recipe's floor beats a lower default
    assert recipes.RECIPES["training-loss-plateau"].source_hint == "screen"
    with pytest.raises(rules.RuleValidationError):
        recipes.build_rule(recipe, "camera:1", roi=[0.9, 0.9, 0.5, 0.5])
