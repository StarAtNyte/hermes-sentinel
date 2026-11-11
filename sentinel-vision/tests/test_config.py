"""Settings parsing (config_schema keys)."""

from __future__ import annotations

import pytest
import yaml
from harness.plugin_loader import PLUGIN_DIR, plugin_module

config = plugin_module("sentinel.config")
SentinelConfig = config.SentinelConfig


def load(values):
    return SentinelConfig.load(lambda key, default=None: values.get(key, default))


def test_config_keys_match_manifest_schema():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    schema = manifest["config_schema"]
    plain = {key: spec for key, spec in schema.items() if spec["type"] != "secret"}
    assert set(plain) == set(config.CONFIG_KEYS)
    defaults = SentinelConfig()
    for key, spec in plain.items():
        assert spec.get("description"), key
        assert spec["default"] == getattr(defaults, key), key


def test_secret_slots_match_the_declared_manifest_entries():
    sources = plugin_module("sentinel.sources")
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    slots = {s["env"] for s in manifest["config_schema"].values() if s["type"] == "secret"}
    assert slots == set(sources.STREAM_URL_SLOTS)


def test_defaults_without_host():
    cfg = SentinelConfig.load(None)
    assert cfg == SentinelConfig()
    assert cfg.problems == ()
    assert cfg.rule_defaults() == {"confirm_k": 2, "confirm_n": 3, "confidence_threshold": 0.7}
    assert cfg.stale_after_seconds == 900.0


def test_valid_values():
    cfg = load(
        {
            "default_source": " screen:1 ",
            "vision_calls_per_hour": "30",
            "vision_calls_per_day": 100,
            "confirm_k": 3,
            "confirm_n": 5,
            "confidence_threshold": 0.8,
            "idle_check_seconds": 600,
            "retention_days": 2,
            "max_storage_mb": 50.5,
            "alert_snapshots": "no",
            "stream_urls": {"front-door": "rtsp://cam/${CAM_PW}"},
        }
    )
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
        ("vision_calls_per_hour", -5),
        ("vision_calls_per_hour", 1.5),
        ("vision_calls_per_hour", "lots"),
        ("vision_calls_per_hour", [1]),
        ("confirm_k", 0),
        ("confirm_n", 99),
        ("confidence_threshold", 1.5),
        ("confidence_threshold", float("nan")),
        ("idle_check_seconds", -1),
        ("retention_days", "x"),
        ("alert_snapshots", "maybe"),
        ("default_source", ""),
        ("default_source", True),
        ("stream_urls", ["rtsp://x"]),
        ("max_storage_mb", True),
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
    cfg = load(
        {
            "stream_urls": {
                "ok": "rtsp://a",
                "bad name": "rtsp://b",
                "": "x",
                "n": 5,
                "long": "x" * 5000,
                "empty": " ",
            }
        }
    )
    assert cfg.stream_urls == {"ok": "rtsp://a"}
    assert len(cfg.problems) == 5
    many = {f"s{i}": "rtsp://x" for i in range(100)}
    assert len(load({"stream_urls": many}).stream_urls) == config.MAX_STREAMS


def test_broken_getter_never_raises():
    def get(key, default=None):
        raise KeyError(key)

    cfg = SentinelConfig.load(get)
    assert cfg.vision_calls_per_hour == 60 and len(cfg.problems) == len(config.CONFIG_KEYS)


def test_judging_defaults():
    cfg = load({})
    assert (cfg.motion_frames, cfg.batch_judging, cfg.batch_max_rules) == (3, True, 4)
    assert cfg.second_opinion is True
    assert (cfg.second_opinion_band_low, cfg.second_opinion_band_high) == (0.5, 0.8)
    assert cfg.second_opinion_per_hour == 6 and cfg.problems == ()


def test_judging_values_are_read():
    cfg = load(
        {
            "motion_frames": 2,
            "batch_judging": "off",
            "batch_max_rules": 6,
            "second_opinion": False,
            "second_opinion_band_low": 0.6,
            "second_opinion_band_high": 0.9,
            "second_opinion_per_hour": 0,
        }
    )
    assert (cfg.motion_frames, cfg.batch_judging, cfg.batch_max_rules) == (2, False, 6)
    assert (cfg.second_opinion, cfg.second_opinion_per_hour) == (False, 0)
    assert (cfg.second_opinion_band_low, cfg.second_opinion_band_high) == (0.6, 0.9)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("motion_frames", 9),
        ("batch_max_rules", 0),
        ("batch_max_rules", 99),
        ("second_opinion_per_hour", -1),
        ("second_opinion_band_high", 2),
        ("batch_judging", "maybe"),
    ],
)
def test_bad_judging_values_fall_back(key, value):
    cfg = load({key: value})
    assert cfg.problems and getattr(cfg, key) == getattr(SentinelConfig(), key)


def test_an_empty_band_is_replaced_by_the_default():
    cfg = load({"second_opinion_band_low": 0.9, "second_opinion_band_high": 0.6})
    assert (cfg.second_opinion_band_low, cfg.second_opinion_band_high) == (0.5, 0.8)
    assert any("band" in problem for problem in cfg.problems)


def test_pacing_defaults_and_values():
    assert (SentinelConfig().rule_min_interval_seconds, SentinelConfig().rule_calls_per_hour) == (
        10.0,
        30,
    )
    cfg = load({"rule_min_interval_seconds": "2.5", "rule_calls_per_hour": 100})
    assert (cfg.rule_min_interval_seconds, cfg.rule_calls_per_hour) == (2.5, 100)
    assert cfg.problems == ()
    assert SentinelConfig().latched_min_interval_seconds == 60.0
    assert load({"latched_min_interval_seconds": "120"}).latched_min_interval_seconds == 120.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("rule_min_interval_seconds", -1),
        ("rule_min_interval_seconds", 99999),
        ("latched_min_interval_seconds", 5),
        ("latched_min_interval_seconds", 99999),
        ("rule_calls_per_hour", 0),
        ("rule_calls_per_hour", 5000),
    ],
)
def test_bad_pacing_values_fall_back(key, value):
    cfg = load({key: value})
    assert cfg.problems and getattr(cfg, key) == getattr(SentinelConfig(), key)
