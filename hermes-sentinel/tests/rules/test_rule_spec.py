"""RuleSpec validation and JSON round-trip."""

from __future__ import annotations

import json

import pytest
from harness.plugin_loader import plugin_module

rules = plugin_module("sentinel.rules")
RuleSpec = rules.RuleSpec
Schedule = rules.Schedule
RVE = rules.RuleValidationError


def spec(**kw):
    base = {"question": "Is the mug on the desk?", "source": "webcam:0"}
    base.update(kw)
    return RuleSpec(**base).validate()


# --------------------------------------------------------------------------- RuleSpec


def test_defaults():
    s = spec()
    assert s.mode == "trigger" and s.action == "notify"
    assert (s.confirm_k, s.confirm_n, s.confidence_threshold) == (2, 3, 0.7)
    assert s.cooldown_seconds == 300.0 and s.hold_seconds == 0.0
    assert s.enabled is True and s.compiled_by == "manual"
    assert s.id.startswith("r") and len(s.id) == 9
    assert s.created_at.endswith("Z")


def test_ids_unique():
    assert len({rules.new_rule_id() for _ in range(200)}) == 200


def test_json_round_trip_full():
    s = spec(
        text="tell me if the mug goes",
        mode="absence",
        hold_seconds=600,
        cooldown_seconds=60,
        roi=(0.1, 0.2, 0.3, 0.4),
        schedule=Schedule(start="22:00", end="06:00", days=(4, 0)),
        action="agent",
        action_prompt="Turn on the lights",
        confirm_k=3,
        confirm_n=5,
        confidence_threshold=0.85,
        enabled=False,
        recipe="desk-intruder",
        compiled_by="llm",
        origin={"session_id": "abc", "platform": "telegram", "chat": 42},
    )
    text = s.to_json()
    data = json.loads(text)
    assert data["roi"] == [0.1, 0.2, 0.3, 0.4]
    assert data["schedule"] == {"start": "22:00", "end": "06:00", "days": [0, 4]}
    back = RuleSpec.from_json(text)
    assert back == s
    assert back.to_json() == text


def test_from_dict_ignores_unknown_keys_and_null_optionals():
    s = RuleSpec.from_dict(
        {
            "question": "q?",
            "source": "s",
            "future_field": 1,
            "id": None,
            "origin": None,
            "created_at": None,
            "schedule": None,
        }
    )
    assert s.origin == {} and s.id.startswith("r") and s.schedule is None


@pytest.mark.parametrize("missing", ["question", "source"])
def test_from_dict_requires_fields(missing):
    data = {"question": "q", "source": "s"}
    del data[missing]
    with pytest.raises(RVE, match=missing):
        RuleSpec.from_dict(data)


@pytest.mark.parametrize("bad", ["not json", "[1,2]", "null", b"\xff"])
def test_from_json_malformed(bad):
    with pytest.raises(RVE):
        RuleSpec.from_json(bad)


def test_rule_validation_error_is_value_error():
    assert issubclass(RVE, ValueError)


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "  "},
        {"question": "x" * 501},
        {"question": 5},
        {"source": ""},
        {"text": "x" * 2001},
        {"text": None},
        {"mode": "sometimes"},
        {"hold_seconds": -1},
        {"hold_seconds": float("nan")},
        {"hold_seconds": "5"},
        {"hold_seconds": True},
        {"hold_seconds": 8 * 86400},
        {"cooldown_seconds": -5},
        {"cooldown_seconds": float("inf")},
        {"roi": (0, 0, 0, 0.5)},
        {"roi": (0.5, 0.5, 0.6, 0.1)},
        {"roi": (1.0, 0, 0.1, 0.1)},
        {"roi": (0, 0, 1)},
        {"roi": "0,0,1,1"},
        {"roi": {"x": 0, "y": 0, "w": 1}},
        {"action": "email"},
        {"action": "agent", "action_prompt": " "},
        {"action_prompt": "x" * 2001},
        {"confirm_k": 0},
        {"confirm_k": 4},
        {"confirm_n": 0},
        {"confirm_n": 21},
        {"confirm_k": 1.5},
        {"confirm_n": True},
        {"confidence_threshold": 1.1},
        {"confidence_threshold": -0.1},
        {"enabled": "yes"},
        {"recipe": ""},
        {"compiled_by": "magic"},
        {"origin": [1]},
        {"origin": {"": 1}},
        {"origin": {"k": {"nested": 1}}},
        {"origin": {"k": "x" * 513}},
        {"origin": {f"k{i}": i for i in range(33)}},
        {"id": "has space"},
        {"id": ""},
        {"created_at": ""},
        {"schedule": {"start": "25:00"}},
    ],
)
def test_validation_rejects(changes):
    with pytest.raises(RVE):
        spec(**changes)


def test_validation_normalises():
    s = spec(
        question="  Is it?  ",
        source=" cam ",
        hold_seconds=5,
        confirm_k=2.0,
        roi=[0.5, 0.5, 0.5, 0.5],
        schedule={"start": "08:00", "end": "17:00", "days": ["mon", "Fri"]},
    )
    assert s.question == "Is it?" and s.source == "cam"
    assert isinstance(s.hold_seconds, float) and s.confirm_k == 2
    assert s.roi == (0.5, 0.5, 0.5, 0.5)
    assert s.schedule.days == (0, 4)


def test_roi_from_mapping_and_tiny_overflow_clamped():
    s = spec(roi={"x": 0.5, "y": 0.0, "w": 0.5000000001, "h": 1.0})
    assert s.roi == (0.5, 0.0, 0.5, 1.0)


def test_effective_hold():
    assert spec(mode="trigger", hold_seconds=60).effective_hold_seconds == 0
    assert spec(mode="condition", hold_seconds=60).effective_hold_seconds == 60
    assert spec(mode="absence", hold_seconds=60).effective_hold_seconds == 60


def test_with_changes_returns_validated_copy():
    s = spec(origin={"a": 1})
    s2 = s.with_changes(enabled=False, confidence_threshold=0.9)
    assert s.enabled is True and s2.enabled is False and s2.id == s.id
    s2.origin["b"] = 2
    assert "b" not in s.origin
    with pytest.raises(RVE):
        s.with_changes(confidence_threshold=3)
    with pytest.raises(RVE, match="unknown"):
        s.with_changes(colour="red")
    with pytest.raises(RVE, match="id"):
        s.with_changes(id="rother")


# --------------------------------------------------------------------------- Schedule
