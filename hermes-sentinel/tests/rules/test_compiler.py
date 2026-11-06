"""compile_rule (LLM + fallback) and fallback_rule."""

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


@pytest.mark.parametrize(
    "text, question_part",
    [
        ("tell me if someone takes my coffee mug", "someone takes my coffee mug"),
        ("Let me know when a package is left at the door.", "a package is left at the door"),
        ("please alert me when the printer fails", "the printer fails"),
        ("watch for a cat on the couch", "a cat on the couch"),
        ("someone at the door", "someone at the door"),
        ("Can you notify me if the light turns red?", "the light turns red"),
    ],
)
def test_fallback_strips_request_prefix(text, question_part):
    s = rules.fallback_rule(text, "cam")
    assert s.question == f"Does the image show this: {question_part}?"
    assert s.mode == "trigger" and s.compiled_by == "fallback"
    assert s.text == " ".join(text.split())


@pytest.mark.parametrize(
    "text, hold",
    [
        ("tell me if the stove is on for 5 minutes", 300),
        ("alert me if the dog is not on its bed for 10 min", 600),
        ("tell me if the door stays open for an hour", 3600),
        ("tell me if the queue is long for more than 30 s", 30),
        ("notify me when nobody is at the desk for 1.5 hours", 5400),
        ("tell me if the stove is on for 1000 hours", 7 * 86400),
    ],
)
def test_fallback_duration_makes_condition(text, hold):
    s = rules.fallback_rule(text, "cam")
    assert s.mode == "condition" and s.hold_seconds == hold
    assert " for " not in s.question.lower()  # the duration phrase is removed


def test_fallback_long_text_is_bounded():
    s = rules.fallback_rule("x " * 5000, "cam")
    assert len(s.text) <= rules.MAX_TEXT_LEN
    assert len(s.question) <= rules.MAX_QUESTION_LEN and s.question.endswith("?")


def test_fallback_applies_defaults_and_origin():
    s = rules.fallback_rule(
        "a mug",
        "cam",
        defaults={
            "confirm_k": 1,
            "confirm_n": 2,
            "confidence_threshold": 0.9,
            "cooldown_seconds": 30,
            "unrelated": 1,
        },
        origin={"session_id": "s1"},
    )
    assert (s.confirm_k, s.confirm_n, s.confidence_threshold, s.cooldown_seconds) == (
        1,
        2,
        0.9,
        30.0,
    )
    assert s.origin == {"session_id": "s1"}


def test_fallback_ignores_invalid_defaults():
    s = rules.fallback_rule("a mug", "cam", defaults={"confirm_k": 9, "confirm_n": 3})
    assert (s.confirm_k, s.confirm_n) == (2, 3)


@pytest.mark.parametrize("text", ["", "   ", None, 5])
def test_compile_rejects_empty_text(text):
    with pytest.raises(ValueError):
        rules.compile_rule(text, "cam", None)


@pytest.mark.parametrize("source", ["", "  ", None])
def test_compile_rejects_empty_source(source):
    with pytest.raises(ValueError):
        rules.compile_rule("a mug", source, None)


def test_compile_without_llm_uses_fallback():
    s = rules.compile_rule("tell me if the mug goes", "cam", None)
    assert s.compiled_by == "fallback"


# --------------------------------------------------------------------------- LLM path


@pytest.fixture
def llm_ctx(fake_ctx):
    fake_ctx.register_auxiliary_task(rules.RULES_TASK, **rules.RULES_TASK_INFO)
    return fake_ctx


GOOD = {
    "question": "Is the coffee mug missing from the desk?",
    "mode": "trigger",
    "hold_seconds": 0,
    "cooldown_seconds": None,
    "roi": None,
    "schedule": None,
    "action": "notify",
    "action_prompt": None,
}


def test_rules_task_key_is_valid_and_not_builtin(fake_ctx):
    fake_ctx.register_auxiliary_task(rules.RULES_TASK, **rules.RULES_TASK_INFO)
    assert rules.RULES_TASK in fake_ctx.aux_tasks
    assert not fake_ctx.warnings


def test_compile_with_llm(llm_ctx):
    llm_ctx.llm.script(GOOD, purpose="sentinel:compile")
    s = rules.compile_rule(
        "tell me if someone takes my mug", "webcam:0", llm_ctx.llm, origin={"session_id": "s"}
    )
    assert s.compiled_by == "llm"
    assert s.question == GOOD["question"] and s.mode == "trigger"
    assert s.source == "webcam:0" and s.text == "tell me if someone takes my mug"
    assert s.origin == {"session_id": "s"}
    (call,) = llm_ctx.llm.calls
    assert call.task == rules.RULES_TASK and call.purpose == "sentinel:compile"
    assert "someone takes my mug" in call.blocks[0].text
    assert not call.images


def test_compile_full_fields(llm_ctx):
    llm_ctx.llm.script(
        {
            **GOOD,
            "question": "Is the dog on its bed?",
            "mode": "absence",
            "hold_seconds": 600,
            "cooldown_seconds": 1200,
            "roi": [0, 0.5, 0.5, 0.5],
            "schedule": {"start": "22:00", "end": "06:00", "days": None},
            "action": "agent",
            "action_prompt": "  Play   a sound  ",
        }
    )
    s = rules.compile_rule(
        "dog off bed 10 min at night", "cam", llm_ctx.llm, defaults={"cooldown_seconds": 60}
    )
    assert s.mode == "absence" and s.hold_seconds == 600
    assert s.cooldown_seconds == 1200  # explicit request beats config default
    assert s.roi == (0.0, 0.5, 0.5, 0.5)
    assert s.schedule == Schedule(start="22:00", end="06:00")
    assert s.action == "agent" and s.action_prompt == "Play a sound"


def test_compile_defaults_used_when_llm_omits(llm_ctx):
    llm_ctx.llm.script(GOOD)
    s = rules.compile_rule(
        "x", "cam", llm_ctx.llm, defaults={"cooldown_seconds": 60, "confidence_threshold": 0.8}
    )
    assert s.cooldown_seconds == 60 and s.confidence_threshold == 0.8


def test_compile_trigger_with_hold_becomes_condition(llm_ctx):
    llm_ctx.llm.script({**GOOD, "hold_seconds": 120})
    s = rules.compile_rule("x", "cam", llm_ctx.llm)
    assert s.mode == "condition" and s.hold_seconds == 120


def test_compile_salvages_bad_optional_fields(llm_ctx):
    llm_ctx.llm.script(
        {
            **GOOD,
            "roi": [0.9, 0.9, 0.5, 0.5],
            "schedule": {"start": "99:00"},
            "hold_seconds": -5,
            "cooldown_seconds": 10**9,
            "action": "agent",
            "action_prompt": None,
        }
    )
    s = rules.compile_rule("x", "cam", llm_ctx.llm)
    assert s.compiled_by == "llm"
    assert s.roi is None and s.schedule is None and s.hold_seconds == 0
    assert s.cooldown_seconds == rules.MAX_COOLDOWN_SECONDS
    assert s.action == "notify"  # agent without a prompt degrades to notify


def test_compile_long_question_truncated(llm_ctx):
    llm_ctx.llm.script({**GOOD, "question": "Is " + "very " * 200 + "big?"})
    s = rules.compile_rule("x", "cam", llm_ctx.llm)
    assert len(s.question) <= rules.MAX_QUESTION_LEN and s.compiled_by == "llm"


def test_compile_fenced_json(llm_ctx):
    llm_ctx.llm.script("```json\n" + json.dumps(GOOD) + "\n```")
    assert rules.compile_rule("x", "cam", llm_ctx.llm).compiled_by == "llm"


@pytest.mark.parametrize(
    "response",
    [
        "I cannot help with that",  # not JSON -> parsed None
        {"question": "", "mode": "trigger"},  # empty question
        {"question": "q?", "mode": "bogus"},  # schema mismatch -> ValueError from host
        {"mode": "trigger"},  # missing question -> schema mismatch
        [1, 2, 3],
        RuntimeError("provider down"),
        TimeoutError("slow"),
    ],
)
def test_compile_falls_back_on_bad_llm_output(llm_ctx, response):
    llm_ctx.llm.script(response)
    s = rules.compile_rule("tell me if the mug goes", "cam", llm_ctx.llm)
    assert s.compiled_by == "fallback"
    assert s.question == "Does the image show this: the mug goes?"


def test_compile_falls_back_when_task_not_registered(fake_ctx):
    fake_ctx.llm.script(GOOD)
    s = rules.compile_rule("a mug", "cam", fake_ctx.llm)
    assert s.compiled_by == "fallback"
    assert fake_ctx.llm.rejected


def test_compile_custom_task_none_uses_default_route(fake_ctx):
    fake_ctx.llm.script(GOOD)
    s = rules.compile_rule("a mug", "cam", fake_ctx.llm, task=None)
    assert s.compiled_by == "llm"


def test_compile_with_object_lacking_method_falls_back():
    assert rules.compile_rule("a mug", "cam", object()).compiled_by == "fallback"


def test_compile_schema_is_valid_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(rules.COMPILE_SCHEMA)
    jsonschema.validate(GOOD, rules.COMPILE_SCHEMA)
