"""FakeLlm must reject exactly what agent/plugin_llm.py rejects (harness honesty checks)."""

from __future__ import annotations

import sys
import threading

import pytest
from harness.fake_ctx import (
    FakeCtx,
    PluginLlmCompleteResult,
    PluginLlmImageInput,
    PluginLlmStructuredResult,
    PluginLlmTextInput,
    PluginLlmTrustError,
    UnscriptedLlmCall,
)

VERDICT = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["match", "confidence"],
    "additionalProperties": False,
}
IMG = PluginLlmImageInput(data=b"\xff\xd8" + b"0" * 98, mime_type="image/jpeg")


def _ask(ctx: FakeCtx, **overrides):
    kwargs = dict(instructions="is the mug there?", input=[IMG], json_schema=VERDICT,
                  purpose="judge")
    kwargs.update(overrides)
    return ctx.llm.complete_structured(**kwargs)


# ---------------------------------------------------------------- scripting + parsing


def test_scripted_dict_is_parsed_and_images_recorded(fake_ctx: FakeCtx):
    fake_ctx.llm.script({"match": True, "confidence": 0.9}, purpose="judge")
    result = _ask(fake_ctx, input=[IMG, {"type": "text", "text": "frame 1"}])
    assert isinstance(result, PluginLlmStructuredResult)
    assert result.parsed == {"match": True, "confidence": 0.9}
    assert result.content_type == "json"
    assert result.agent_id == "default"
    assert result.audit == {"plugin_id": "hermes-sentinel", "purpose": "judge", "profile": "",
                            "task": "", "schema_name": ""}
    call = fake_ctx.llm.calls[0]
    assert call.image_sizes == [100]
    assert [b.type for b in call.blocks] == ["image", "text"]
    assert call.blocks[1].text == "frame 1"


def test_unscripted_call_raises(fake_ctx: FakeCtx):
    with pytest.raises(UnscriptedLlmCall):
        _ask(fake_ctx)


def test_lookup_order_and_responder_fallback(fake_ctx: FakeCtx):
    llm = fake_ctx.llm
    llm.script({"match": True, "confidence": 1.0}, purpose="judge")
    llm.respond(lambda call: {"match": False, "confidence": 0.1})
    assert _ask(fake_ctx).parsed["match"] is True  # exact queue first
    assert _ask(fake_ctx).parsed["match"] is False  # then the (None, None) responder
    assert len(llm.calls_for(purpose="judge")) == 2


def test_fenced_json_is_parsed(fake_ctx: FakeCtx):
    fake_ctx.llm.script('```json\n{"match": false, "confidence": 0.2}\n```')
    assert _ask(fake_ctx).parsed == {"match": False, "confidence": 0.2}


def test_invalid_json_returns_text(fake_ctx: FakeCtx):
    fake_ctx.llm.script("I think so")
    result = _ask(fake_ctx)
    assert result.parsed is None and result.content_type == "text" and result.text == "I think so"


def test_no_json_requested_means_no_parse(fake_ctx: FakeCtx):
    fake_ctx.llm.script({"match": True})
    result = _ask(fake_ctx, json_schema=None)
    assert result.parsed is None and result.content_type == "text"


def test_json_mode_without_schema_parses_anything(fake_ctx: FakeCtx):
    fake_ctx.llm.script({"anything": 1})
    assert _ask(fake_ctx, json_schema=None, json_mode=True).parsed == {"anything": 1}


@pytest.mark.parametrize(
    "bad",
    [
        {"match": "yes", "confidence": 0.5},
        {"match": True},
        {"match": True, "confidence": 1.5},
        {"match": True, "confidence": 0.5, "extra": 1},
    ],
)
def test_schema_mismatch_raises_value_error(fake_ctx: FakeCtx, bad):
    fake_ctx.llm.script(bad)
    with pytest.raises(ValueError, match="did not match schema"):
        _ask(fake_ctx)


def test_exception_and_prebuilt_results(fake_ctx: FakeCtx):
    fake_ctx.llm.script(TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        _ask(fake_ctx)
    prebuilt = PluginLlmStructuredResult(text="x", provider="p", model="m", agent_id="a")
    fake_ctx.llm.script(prebuilt)
    assert _ask(fake_ctx) is prebuilt


def test_complete_text(fake_ctx: FakeCtx):
    fake_ctx.llm.script("a rule", purpose="compile")
    result = fake_ctx.llm.complete([{"role": "user", "content": "hi"}], purpose="compile")
    assert isinstance(result, PluginLlmCompleteResult) and result.text == "a rule"


async def test_async_variants(fake_ctx: FakeCtx):
    fake_ctx.llm.script({"match": True, "confidence": 0.5}, "plain")
    assert (await fake_ctx.llm.acomplete_structured(
        instructions="q", input=[IMG], json_schema=VERDICT)).parsed["match"] is True
    assert (await fake_ctx.llm.acomplete([{"role": "user", "content": "x"}])).text == "plain"
    with pytest.raises(ValueError):
        await fake_ctx.llm.acomplete_structured(instructions="", input=[IMG])


# ---------------------------------------------------------------- argument checks


@pytest.mark.parametrize("instructions", ["", "   ", None])
def test_empty_instructions_rejected(fake_ctx: FakeCtx, instructions):
    with pytest.raises(ValueError, match="non-empty instructions"):
        _ask(fake_ctx, instructions=instructions)


def test_empty_input_rejected(fake_ctx: FakeCtx):
    with pytest.raises(ValueError, match="at least one input block"):
        _ask(fake_ctx, input=[])


@pytest.mark.parametrize(
    "block",
    [
        PluginLlmImageInput(data="not bytes"),  # type: ignore[arg-type]
        PluginLlmImageInput(data=memoryview(b"abc")),  # type: ignore[arg-type]
        {"type": "image", "data": "base64?"},
        {"type": "image"},
        {"type": "text", "text": 3},
        {"type": "audio", "data": b""},
        "a bare string",
        42,
    ],
)
def test_bad_input_blocks_rejected(fake_ctx: FakeCtx, block):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    with pytest.raises(ValueError):
        _ask(fake_ctx, input=[block])
    assert list(fake_ctx.llm.calls) == []


@pytest.mark.parametrize(
    "block",
    [
        PluginLlmImageInput(data=bytearray(b"12")),
        PluginLlmImageInput(url="https://example.com/x.png"),
        {"type": "image", "url": "data:image/png;base64,AAAA"},
        {"type": "image", "data": b"raw"},
        PluginLlmTextInput(text="context"),
    ],
)
def test_good_input_blocks_accepted(fake_ctx: FakeCtx, block):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    assert _ask(fake_ctx, input=[block]).parsed["match"] is True


# ---------------------------------------------------------------- trust gate


def test_unregistered_task_rejected_registered_task_allowed(fake_ctx: FakeCtx):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    with pytest.raises(PluginLlmTrustError, match="did not register|registered itself"):
        _ask(fake_ctx, task="sentinel_vision")
    fake_ctx.register_auxiliary_task("sentinel_vision", display_name="Sentinel vision",
                                     description="frame judge")
    result = _ask(fake_ctx, task="sentinel_vision")
    assert result.audit["task"] == "sentinel_vision"
    assert [m for m, _ in fake_ctx.llm.rejected] == ["complete_structured"]


@pytest.mark.parametrize("task", [None, "", "auto", "AUTO"])
def test_auto_task_is_main_model(fake_ctx: FakeCtx, task):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    assert _ask(fake_ctx, task=task).audit["task"] == ""


def test_builtin_task_needs_allow_task_override(fake_ctx: FakeCtx):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    with pytest.raises(PluginLlmTrustError, match="allow_task_override"):
        _ask(fake_ctx, task="vision")
    fake_ctx.entry["llm"] = {"allow_task_override": True}
    assert _ask(fake_ctx, task="vision").audit["task"] == "vision"


@pytest.mark.parametrize(
    ("override", "flag"),
    [
        ({"provider": "openrouter"}, "allow_provider_override"),
        ({"model": "gpt-x"}, "allow_model_override"),
        ({"agent_id": "other"}, "allow_agent_id_override"),
        ({"profile": "work"}, "allow_profile_override"),
    ],
)
def test_overrides_need_trust(fake_ctx: FakeCtx, override, flag):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    with pytest.raises(PluginLlmTrustError, match=flag):
        _ask(fake_ctx, **override)
    with pytest.raises(PluginLlmTrustError):
        fake_ctx.llm.complete([{"role": "user", "content": "x"}], **override)
    fake_ctx.entry["llm"] = {flag: True}
    result = _ask(fake_ctx, **override)
    if "provider" in override:
        assert result.provider == "openrouter"
    if "model" in override:
        assert result.model == "gpt-x"
    if "agent_id" in override:
        assert result.agent_id == "other"
    if "profile" in override:
        assert result.audit["profile"] == "work"


def test_model_allowlist(fake_ctx: FakeCtx):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    fake_ctx.entry["llm"] = {"allow_model_override": True, "allowed_models": ["Vision-1"]}
    assert _ask(fake_ctx, model="vision-1").model == "vision-1"
    with pytest.raises(PluginLlmTrustError, match="allowed_models"):
        _ask(fake_ctx, model="other")
    fake_ctx.entry["llm"]["allowed_models"] = ["*"]
    assert _ask(fake_ctx, model="other").model == "other"


def test_check_order_matches_host(fake_ctx: FakeCtx):
    # Argument errors come before trust errors; trust errors before input-shape errors.
    with pytest.raises(ValueError):
        _ask(fake_ctx, instructions="", task="nope")
    with pytest.raises(PluginLlmTrustError):
        _ask(fake_ctx, task="nope", input=[{"type": "image", "data": "str"}])


def test_trust_error_is_the_shimmed_hermes_class(fake_ctx: FakeCtx):
    from agent.plugin_llm import PluginLlmImageInput as ShimImage
    from agent.plugin_llm import PluginLlmTrustError as ShimTrust

    assert ShimTrust is PluginLlmTrustError and issubclass(ShimTrust, PermissionError)
    assert ShimImage is PluginLlmImageInput
    assert sys.modules["agent.plugin_llm"].PluginLlmStructuredResult is PluginLlmStructuredResult


def test_thread_safe_recording(fake_ctx: FakeCtx):
    fake_ctx.llm.respond(lambda call: {"match": True, "confidence": 1})
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(25):
                _ask(fake_ctx)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(fake_ctx.llm.calls) == 100


def test_reset_clears_everything(fake_ctx: FakeCtx):
    fake_ctx.llm.script({"match": True, "confidence": 1})
    _ask(fake_ctx)
    fake_ctx.llm.reset()
    assert list(fake_ctx.llm.calls) == []
    with pytest.raises(UnscriptedLlmCall):
        _ask(fake_ctx)
    with pytest.raises(TypeError):
        fake_ctx.llm.respond("not callable")  # type: ignore[arg-type]
