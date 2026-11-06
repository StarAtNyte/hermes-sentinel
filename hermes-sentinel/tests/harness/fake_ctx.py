"""In-process stand-in for Hermes' ``PluginContext``: unit tests run without a live Hermes.

Mirrors the public surface of ``hermes_cli.plugins.PluginContext`` and
``agent.plugin_llm.PluginLlm``
(method and parameter names identical) and enforces the same checks the host does:

* ``register_tool``: malformed schemas raise ``ValueError``; ``override=True`` without the
  ``tools.override`` capability raises :class:`PluginToolOverrideError`; shadowing a built-in
  tool (names parsed from the Hermes source, see :mod:`harness.hermes_surface`) without
  ``override`` is refused (returns ``None`` and records a warning).
* ``register_command``: empty names and names that clash with a built-in slash command are
  skipped (``None`` + warning), like the real one.
* ``register_hook`` / ``register_middleware``: names outside the real ``VALID_HOOKS`` /
  ``VALID_MIDDLEWARE`` are stored but warned about (as Hermes does); tests assert
  ``ctx.warnings == []``.
* ``register_auxiliary_task``: key validation, reserved built-in keys, ``inherit_from`` checks.
* ``get_config`` / ``set_config``: plugin-relative key validation (``ValueError``).
* ``spawn_task``: needs a running loop (``RuntimeError`` otherwise), exactly like the host. The
  controllable loop is only reachable through :meth:`FakeCtx.run`, :meth:`FakeCtx.call_in_loop`
  and :meth:`FakeCtx.run_until_idle`.
* ``ctx.llm`` -> :class:`FakeLlm`: argument checks, trust gate (task + overrides, configured via
  the plugin's ``entry["llm"]`` block), input-block normalisation (image ``data`` must be bytes)
  and JSON-schema validation of parsed output, then a scripted response.

Storage and secrets are *module functions* in Hermes (``plugins.plugin_storage``,
``agent.secret_scope``), not ``ctx`` methods; :mod:`harness.hermes_shims` provides them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .hermes_surface import HermesSurface, surface
from .llm_types import (
    PluginLlmCompleteResult,
    PluginLlmImageInput,
    PluginLlmInput,
    PluginLlmStructuredResult,
    PluginLlmTextInput,
    PluginLlmTrustError,
    PluginLlmUsage,
)

__all__ = [
    "FakeCtx",
    "FakeLlm",
    "FakePostHandle",
    "FakeRegistration",
    "InjectedMessage",
    "InputBlock",
    "LlmCall",
    "PluginLlmCompleteResult",
    "PluginLlmImageInput",
    "PluginLlmStructuredResult",
    "PluginLlmTextInput",
    "PluginLlmTrustError",
    "PluginLlmUsage",
    "PluginToolOverrideError",
    "UnscriptedLlmCall",
]

logger = logging.getLogger(__name__)

RECORD_LIMIT = 10_000  # cap on every recorder list, so long engine tests cannot grow without bound

# Capability ids and their legacy ``plugins.entries.<id>`` keys (hermes_cli/plugin_capabilities.py).
CAPABILITY_LEGACY_PATHS: dict[str, tuple[str, ...]] = {
    "tools.override": ("allow_tool_override",),
    "llm.provider_override": ("llm", "allow_provider_override"),
    "llm.model_override": ("llm", "allow_model_override"),
    "llm.agent_id_override": ("llm", "allow_agent_id_override"),
    "llm.profile_override": ("llm", "allow_profile_override"),
    "llm.task_override": ("llm", "allow_task_override"),
    "gateway.platform_actions": ("allow_platform_actions",),
}

# hermes_cli/plugins_state.py
_SETTING_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SETTING_RESERVED_ROOTS = frozenset({"model", "plugins", "security", "settings"})


class PluginToolOverrideError(PermissionError):
    """Plugin tried to override a built-in tool without ``allow_tool_override``."""


class UnscriptedLlmCall(AssertionError):
    """Raised when the plugin calls the LLM and no scripted response matches."""


# --------------------------------------------------------------------------- records


@dataclass
class FakeRegistration:
    """Stand-in for ``PluginRegistration``: a handle whose ``dispose()`` undoes the registration."""

    kind: str
    name: str
    _dispose: Callable[[], None] | None = None
    disposed: bool = False
    hold_stream: bool = False

    def dispose(self) -> None:
        if self.disposed:
            return
        self.disposed = True
        if self._dispose is not None:
            self._dispose()


@dataclass
class InputBlock:
    """Summary of one normalised ``input`` block passed to the LLM."""

    type: str
    text: str | None = None
    byte_size: int | None = None
    url: str | None = None
    mime_type: str | None = None


@dataclass
class LlmCall:
    """One LLM call that passed the argument and trust checks."""

    method: str
    kwargs: dict[str, Any]
    blocks: list[InputBlock]

    @property
    def purpose(self) -> str | None:
        return self.kwargs.get("purpose")

    @property
    def task(self) -> str | None:
        return self.kwargs.get("task")

    @property
    def images(self) -> list[InputBlock]:
        return [b for b in self.blocks if b.type == "image"]

    @property
    def image_sizes(self) -> list[int | None]:
        return [b.byte_size for b in self.images]


@dataclass
class InjectedMessage:
    content: str
    role: str
    session_key: str | None
    origin: Mapping[str, Any] | None


@dataclass
class FakePostHandle:
    """Shape of the handle ``post_message`` returns (``ok``, ``error``, ``edit(text)``)."""

    text: str
    ok: bool = True
    error: str | None = None
    edits: list[str] = field(default_factory=list)

    def edit(self, text: str) -> FakePostHandle:
        if self.ok:
            self.edits.append(text)
            self.text = text
        return self

    async def aedit(self, text: str) -> FakePostHandle:
        return self.edit(text)


# --------------------------------------------------------------------------- llm checks
# Behaviour copied from agent/plugin_llm.py; keep in step with it.


@dataclass(frozen=True)
class _TrustPolicy:
    plugin_id: str
    allow_provider_override: bool = False
    allowed_providers: frozenset[str] | None = None
    allow_any_provider: bool = False
    allow_model_override: bool = False
    allowed_models: frozenset[str] | None = None
    allow_any_model: bool = False
    allow_agent_id_override: bool = False
    allow_profile_override: bool = False
    allow_task_override: bool = False


_OVERRIDE_FLAGS = (
    "allow_provider_override",
    "allow_model_override",
    "allow_agent_id_override",
    "allow_profile_override",
    "allow_task_override",
)


def _coerce_allowlist(raw: Any) -> tuple[frozenset[str] | None, bool]:
    if not isinstance(raw, list):
        return None, False
    normalized = [item.strip().lower() for item in raw if isinstance(item, str)]
    return frozenset(i for i in normalized if i and i != "*"), "*" in normalized


def _policy_from_entry(plugin_id: str, llm_cfg: Any) -> _TrustPolicy:
    if not plugin_id or not isinstance(llm_cfg, Mapping):
        return _TrustPolicy(plugin_id=plugin_id)
    allowed_models, any_model = _coerce_allowlist(llm_cfg.get("allowed_models"))
    allowed_providers, any_provider = _coerce_allowlist(llm_cfg.get("allowed_providers"))
    return _TrustPolicy(
        plugin_id=plugin_id,
        allowed_providers=allowed_providers,
        allow_any_provider=any_provider,
        allowed_models=allowed_models,
        allow_any_model=any_model,
        **{name: bool(llm_cfg.get(name, False)) for name in _OVERRIDE_FLAGS},
    )


def _denied(plugin_id: str, what: str, flag: str) -> PluginLlmTrustError:
    return PluginLlmTrustError(
        f"Plugin {plugin_id!r} cannot {what} "
        f"(set plugins.entries.{plugin_id}.llm.{flag} to true to allow)."
    )


def _gate_ref_override(policy: _TrustPolicy, kind: str, requested: str) -> str:
    if not getattr(policy, f"allow_{kind}_override"):
        raise _denied(policy.plugin_id, f"override the {kind}", f"allow_{kind}_override")
    allowed = getattr(policy, f"allowed_{kind}s")
    if (
        not getattr(policy, f"allow_any_{kind}")
        and allowed is not None
        and requested.strip().lower() not in allowed
    ):
        raise PluginLlmTrustError(
            f"Plugin {policy.plugin_id!r} {kind} override {requested!r} is not in "
            f"plugins.entries.{policy.plugin_id}.llm.allowed_{kind}s."
        )
    return requested.strip()


_FLAG_ONLY_OVERRIDES = {
    "agent_id": "run completions against a non-default agent id",
    "profile": "override the auth profile",
}


def _check_overrides(
    policy: _TrustPolicy,
    provider: str | None,
    model: str | None,
    agent_id: str | None,
    profile: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    final_provider = _gate_ref_override(policy, "provider", provider) if provider else None
    final_model = _gate_ref_override(policy, "model", model) if model else None
    for kind, requested in (("agent_id", agent_id), ("profile", profile)):
        if requested and not getattr(policy, f"allow_{kind}_override"):
            raise _denied(policy.plugin_id, _FLAG_ONLY_OVERRIDES[kind], f"allow_{kind}_override")
    return final_provider, final_model, agent_id, profile.strip() if profile else None


def _check_task(
    policy: _TrustPolicy,
    plugin_id: str,
    requested: str | None,
    owned: frozenset[str],
    builtin: frozenset[str],
) -> str | None:
    task = (requested or "").strip()
    if not task or task.lower() == "auto":
        return None
    if task in owned or (task in builtin and policy.allow_task_override):
        return task
    if task in builtin:
        raise _denied(
            plugin_id, f"route through the built-in auxiliary task {task!r}", "allow_task_override"
        )
    raise PluginLlmTrustError(
        f"Plugin {plugin_id!r} cannot route through auxiliary task {task!r} - a plugin may only "
        f"pass a task key it registered itself via ctx.register_auxiliary_task() (or a built-in "
        f"key when plugins.entries.{plugin_id}.llm.allow_task_override is true)."
    )


def _structured_spec(name: str, instructions: Any, inputs: Any) -> None:
    if not instructions or not str(instructions).strip():
        raise ValueError(f"{name} requires non-empty instructions")
    if not inputs:
        raise ValueError(f"{name} requires at least one input block")


def _normalize_input_block(block: PluginLlmInput) -> dict[str, Any]:
    if isinstance(block, PluginLlmTextInput):
        return {"type": "text", "text": block.text}
    if isinstance(block, PluginLlmImageInput):
        d: dict[str, Any] = {"type": "image", "mime_type": block.mime_type,
                             "file_name": block.file_name}
        if block.data is not None:
            d["data"] = block.data
        if block.url:
            d["url"] = block.url
        return d
    if not isinstance(block, dict):
        raise ValueError(f"Unsupported input block: {type(block).__name__}")
    kind = block.get("type")
    if kind == "text":
        if not isinstance(block.get("text"), str):
            raise ValueError("text input block requires 'text' string")
        return {"type": "text", "text": block["text"]}
    if kind == "image":
        if "data" not in block and not block.get("url"):
            raise ValueError("image input block requires 'data' bytes or 'url'")
        return {
            "type": "image",
            "data": block.get("data"),
            "url": block.get("url"),
            "mime_type": block.get("mime_type") or "image/png",
            "file_name": block.get("file_name") or "",
        }
    raise ValueError(f"Unknown input block type: {kind!r}")


def _summarize(norm: dict[str, Any]) -> InputBlock:
    """Summarise a normalised block; raises like ``_image_part`` for non-bytes image data."""
    if norm["type"] == "text":
        return InputBlock(type="text", text=norm["text"])
    url = norm.get("url")
    size: int | None = None
    if not url:
        data = norm.get("data") or b""
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("image input 'data' must be bytes")
        size = len(data)
    return InputBlock(
        type="image", byte_size=size, url=url or None, mime_type=norm.get("mime_type")
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def _parse_structured_text(
    text: str, json_mode: bool, json_schema: Any | None
) -> tuple[Any | None, str]:
    if not (json_mode or json_schema is not None) or not text:
        return None, "text"
    match = _FENCE_RE.search(text)
    body = match.group(1).strip() if match else text.strip()
    try:
        parsed = json.loads(body)
    except ValueError:
        return None, "text"
    if json_schema is not None:
        try:
            jsonschema.validate(parsed, json_schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(
                f"Plugin LLM structured output did not match schema: {exc.message}"
            ) from exc
    return parsed, "json"


# --------------------------------------------------------------------------- FakeLlm

Responder = Callable[[LlmCall], Any]


class FakeLlm:
    """Scripted ``ctx.llm`` that applies the real argument, trust and schema checks.

    ``script(resp, purpose=..., task=...)`` queues one response (FIFO); ``respond(fn, ...)``
    installs a callable ``fn(call) -> resp`` used when that key's queue is empty. Lookup order:
    exact ``(purpose, task)``, then ``(purpose, None)``, ``(None, task)``, ``(None, None)``.

    A response may be a dict/list (serialised to JSON text), a str (raw model text), a
    ``PluginLlmStructuredResult``/``PluginLlmCompleteResult`` (returned as is, skipping parsing)
    or an exception instance (raised). Text is parsed exactly like the host: fences stripped,
    ``parsed`` set only when JSON was requested and valid, ``ValueError`` on a schema mismatch.

    Trust comes from ``policy_source()`` (the ``plugins.entries.<id>.llm`` mapping) and the task
    keys from ``owned_tasks()`` / ``builtin_tasks``; :class:`FakeCtx` wires these up.
    """

    provider = "fake"
    model = "fake-vision"

    def __init__(
        self,
        *,
        plugin_id: str = "fake-plugin",
        policy_source: Callable[[], Any] | None = None,
        owned_tasks: Callable[[], frozenset[str]] | None = None,
        builtin_tasks: frozenset[str] = frozenset(),
    ) -> None:
        self._plugin_id = plugin_id
        self._policy_source = policy_source or (lambda: None)
        self._owned_tasks = owned_tasks or frozenset
        self._builtin_tasks = frozenset(builtin_tasks)
        self._lock = threading.RLock()
        self._queues: dict[tuple[str | None, str | None], deque[Any]] = {}
        self._responders: dict[tuple[str | None, str | None], Responder] = {}
        self.calls: deque[LlmCall] = deque(maxlen=RECORD_LIMIT)
        self.rejected: deque[tuple[str, BaseException]] = deque(maxlen=RECORD_LIMIT)

    # -- scripting
    def script(
        self, *responses: Any, purpose: str | None = None, task: str | None = None
    ) -> FakeLlm:
        with self._lock:
            self._queues.setdefault((purpose, task), deque()).extend(responses)
        return self

    def respond(
        self, fn: Responder, *, purpose: str | None = None, task: str | None = None
    ) -> FakeLlm:
        if not callable(fn):
            raise TypeError("respond() needs a callable")
        with self._lock:
            self._responders[(purpose, task)] = fn
        return self

    def reset(self) -> None:
        with self._lock:
            self._queues.clear()
            self._responders.clear()
            self.calls.clear()
            self.rejected.clear()

    def calls_for(self, *, purpose: str | None = None, task: str | None = None) -> list[LlmCall]:
        with self._lock:
            return [
                c
                for c in self.calls
                if (purpose is None or c.purpose == purpose) and (task is None or c.task == task)
            ]

    # -- pipeline
    def _policy(self) -> _TrustPolicy:
        return _policy_from_entry(self._plugin_id, self._policy_source())

    def _gate(
        self,
        method: str,
        *,
        provider: str | None,
        model: str | None,
        agent_id: str | None,
        profile: str | None,
        task: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        policy = self._policy()
        try:
            eff_task = _check_task(
                policy, self._plugin_id, task, frozenset(self._owned_tasks()), self._builtin_tasks
            )
            return (*_check_overrides(policy, provider, model, agent_id, profile), eff_task)
        except PluginLlmTrustError as exc:
            with self._lock:
                self.rejected.append((method, exc))
            raise

    def _resolve(self, call: LlmCall) -> Any:
        p, t = call.purpose, call.task
        with self._lock:
            for key in ((p, t), (p, None), (None, t), (None, None)):
                queue = self._queues.get(key)
                if queue:
                    return queue.popleft()
                responder = self._responders.get(key)
                if responder is not None:
                    break
            else:
                raise UnscriptedLlmCall(f"no scripted LLM response for purpose={p!r} task={t!r}")
        return responder(call)

    def _base(
        self, call: LlmCall, provider: str | None, model: str | None, agent_id: str | None
    ) -> dict[str, Any]:
        return dict(
            provider=provider or self.provider,
            model=model or self.model,
            agent_id=agent_id or "default",
        )

    def _audit(self, call: LlmCall, profile: str | None, task: str | None) -> dict[str, Any]:
        return {
            "plugin_id": self._plugin_id,
            "purpose": call.purpose or "",
            "profile": profile or "",
            "task": task or "",
        }

    def _record(self, method: str, kwargs: dict[str, Any], blocks: list[InputBlock]) -> LlmCall:
        call = LlmCall(method=method, kwargs=kwargs, blocks=blocks)
        with self._lock:
            self.calls.append(call)
        return call

    # -- public API (signatures mirror agent/plugin_llm.py PluginLlm)
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> PluginLlmCompleteResult:
        eff_provider, eff_model, eff_agent, eff_profile, eff_task = self._gate(
            "complete", provider=provider, model=model, agent_id=agent_id, profile=profile,
            task=task,
        )
        kwargs = dict(
            messages=messages, provider=provider, model=model, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, agent_id=agent_id, profile=profile,
            purpose=purpose, task=task,
        )
        call = self._record("complete", kwargs, [])
        response = self._resolve(call)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, (PluginLlmCompleteResult, PluginLlmStructuredResult)):
            return response  # type: ignore[return-value]
        text = json.dumps(response) if isinstance(response, (dict, list)) else str(response)
        return PluginLlmCompleteResult(
            text=text,
            **self._base(call, eff_provider, eff_model, eff_agent),
            audit=self._audit(call, eff_profile, eff_task),
        )

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> PluginLlmCompleteResult:
        return self.complete(
            messages, provider=provider, model=model, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, agent_id=agent_id, profile=profile,
            purpose=purpose, task=task,
        )

    def complete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[PluginLlmInput],
        json_schema: Any | None = None,
        json_mode: bool = False,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> PluginLlmStructuredResult:
        return self._structured(
            "complete_structured",
            instructions=instructions, input=input, json_schema=json_schema, json_mode=json_mode,
            schema_name=schema_name, system_prompt=system_prompt, provider=provider, model=model,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout, agent_id=agent_id,
            profile=profile, purpose=purpose, task=task,
        )

    async def acomplete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[PluginLlmInput],
        json_schema: Any | None = None,
        json_mode: bool = False,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str | None = None,
        profile: str | None = None,
        purpose: str | None = None,
        task: str | None = None,
    ) -> PluginLlmStructuredResult:
        return self._structured(
            "acomplete_structured",
            instructions=instructions, input=input, json_schema=json_schema, json_mode=json_mode,
            schema_name=schema_name, system_prompt=system_prompt, provider=provider, model=model,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout, agent_id=agent_id,
            profile=profile, purpose=purpose, task=task,
        )

    def _structured(self, method: str, **kw: Any) -> PluginLlmStructuredResult:
        # Order matches the host: argument check, trust gate, then input-shape errors.
        _structured_spec(method, kw["instructions"], kw["input"])
        eff_provider, eff_model, eff_agent, eff_profile, eff_task = self._gate(
            method, provider=kw["provider"], model=kw["model"], agent_id=kw["agent_id"],
            profile=kw["profile"], task=kw["task"],
        )
        inputs = list(kw["input"])
        blocks = [_summarize(_normalize_input_block(b)) for b in inputs]
        kw["input"] = inputs
        call = self._record("complete_structured", kw, blocks)
        response = self._resolve(call)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, (PluginLlmStructuredResult, PluginLlmCompleteResult)):
            return response  # type: ignore[return-value]
        text = json.dumps(response) if isinstance(response, (dict, list)) else str(response)
        parsed, content_type = _parse_structured_text(text, kw["json_mode"], kw["json_schema"])
        audit = self._audit(call, eff_profile, eff_task)
        audit["schema_name"] = kw["schema_name"] or ""
        return PluginLlmStructuredResult(
            text=text,
            parsed=parsed,
            content_type=content_type,
            **self._base(call, eff_provider, eff_model, eff_agent),
            audit=audit,
        )


# --------------------------------------------------------------------------- FakeCtx


def _validate_setting_key(key: Any) -> tuple[str, ...]:
    if not isinstance(key, str):
        raise ValueError("Expected a plugin-relative config key string")
    segments = tuple(key.split("."))
    invalid = (
        not key or "/" in key or "\\" in key or segments[0].lower() in _SETTING_RESERVED_ROOTS
    )
    if invalid or not all(_SETTING_SEGMENT_RE.fullmatch(s) for s in segments):
        raise ValueError(
            "Expected a plugin-relative config key such as 'endpoint' or 'retry.policy'; "
            "global, cross-plugin, and traversal paths are forbidden"
        )
    return segments


class FakeCtx:
    """Recording stand-in for ``PluginContext``.

    Constructor knobs:

    * ``config``: the plugin's ``settings`` subtree (what ``get_config`` reads).
    * ``entry``: the rest of ``plugins.entries.<plugin_id>`` (``allow_tool_override``,
      ``granted_capabilities``, ``llm: {allow_model_override: ...}``, ...). Empty means no trust.
    * ``source``: manifest source; ``"bundled"`` plugins may override tools, like in Hermes.
    * ``host``: the :class:`HermesSurface` to validate names against (default: parsed from the
      Hermes checkout, or the pinned snapshot).

    Recorders: ``tools``, ``hooks``, ``commands``, ``cli_commands``, ``middleware``,
    ``aux_tasks``, ``platform_handlers``, ``injected``, ``posted``, ``choices_asked``,
    ``config_reads``, ``warnings``. Recorders are thread-safe (engine threads may post/inject).
    """

    def __init__(
        self,
        *,
        plugin_id: str = "hermes-sentinel",
        config: dict[str, Any] | None = None,
        entry: Mapping[str, Any] | None = None,
        manifest: Mapping[str, Any] | None = None,
        source: str = "user",
        profile_name: str = "default",
        session_cwd: str = "",
        session_surface: str = "cli",
        inject_result: bool = True,
        post_ok: bool = True,
        gateway_injection_allowed: bool = True,
        classic_cli: bool = False,
        gateway_live: bool = True,
        host: HermesSurface | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self.manifest = dict(manifest or {"name": plugin_id})
        self.source = source
        self.config: dict[str, Any] = dict(config or {})
        self.entry: dict[str, Any] = dict(entry or {})
        self._profile_name = profile_name
        self._session_cwd = session_cwd
        self._session_surface = session_surface
        self.inject_result = inject_result
        self.post_ok = post_ok
        #: ``plugins.entries.<id>.allow_gateway_injection`` (real default: off).
        self.gateway_injection_allowed = gateway_injection_allowed
        #: a classic in-process CLI REPL is attached (it takes any non-origin injection).
        self.classic_cli = classic_cli
        #: a live gateway/TUI message injector exists.
        self.gateway_live = gateway_live
        self.inject_refused: list[tuple[str, str]] = []  # (reason, content)
        self.host = host or surface()
        self._lock = threading.RLock()
        self._llm = FakeLlm(
            plugin_id=plugin_id,
            policy_source=lambda: self.entry.get("llm"),
            owned_tasks=lambda: frozenset(self.aux_tasks),
            builtin_tasks=self.host.aux_tasks,
        )
        self._loop = loop
        self._owns_loop = loop is None

        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Callable]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}
        self.middleware: dict[str, list[Callable]] = {}
        self.aux_tasks: dict[str, dict[str, Any]] = {}
        self.platform_handlers: dict[str, list[Callable]] = {}

        self.injected: deque[InjectedMessage] = deque(maxlen=RECORD_LIMIT)
        self.posted: deque[FakePostHandle] = deque(maxlen=RECORD_LIMIT)
        self.choice_answers: deque[str | None] = deque()
        self.choices_asked: deque[tuple[str, list[Any]]] = deque(maxlen=RECORD_LIMIT)
        self.warnings: deque[str] = deque(maxlen=RECORD_LIMIT)
        self.config_reads: deque[str] = deque(maxlen=RECORD_LIMIT)
        self.tasks: list[asyncio.Task] = []
        self._unload_callbacks: list[Callable[[], None]] = []
        self.unload_errors: list[BaseException] = []
        self.unloaded = False

    # -- identity / environment
    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def llm(self) -> FakeLlm:
        return self._llm

    def has_plugin(self, plugin_id: str) -> bool:
        return False

    def session_cwd(self) -> str:
        return self._session_cwd

    def session_surface(self) -> str:
        return self._session_surface

    def _warn(self, message: str) -> None:
        logger.warning("%s", message)
        with self._lock:
            self.warnings.append(message)

    # -- capabilities
    def has_capability(self, capability: str) -> bool:
        """``granted_capabilities`` or the legacy ``allow_*`` key; unknown ids are denied."""
        if self.source == "bundled" and capability == "tools.override":
            return True
        legacy = CAPABILITY_LEGACY_PATHS.get(capability)
        if legacy is None:
            return False
        granted = self.entry.get("granted_capabilities")
        if isinstance(granted, list) and capability in {
            c.strip() for c in granted if isinstance(c, str)
        }:
            return True
        node: Any = self.entry
        for part in legacy:
            if not isinstance(node, Mapping):
                return False
            node = node.get(part)
        return bool(node)

    def _tool_override_allowed(self) -> bool:
        return self.source == "bundled" or self.has_capability("tools.override")

    # -- config (plugin-relative keys under plugins.entries.<id>.settings)
    def get_config(self, key: str, default: Any = None) -> Any:
        segments = _validate_setting_key(key)
        with self._lock:
            self.config_reads.append(key)
        node: Any = self.config
        for part in segments:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_config(self, key: str, value: Any) -> None:
        parts = _validate_setting_key(key)
        with self._lock:
            node = self.config
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = node[part] = {}
                node = child
            node[parts[-1]] = value

    # -- registration
    def _handle(self, kind: str, name: str, undo: Callable[[], None]) -> FakeRegistration:
        return FakeRegistration(kind=kind, name=name, _dispose=undo)

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        requires_env: list | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> FakeRegistration | None:
        if override and not self._tool_override_allowed():
            raise PluginToolOverrideError(
                f"Plugin {self.manifest.get('name')!r} cannot override built-in tool {name!r}. "
                f"Set plugins.entries.{self.plugin_id}.allow_tool_override: true in config.yaml "
                "to allow this plugin to replace built-in tools."
            )
        if name in self.host.tools and not override:
            self._warn(f"Plugin {self.plugin_id} tried to shadow global tool {name} "
                       "without override=True")
            return None
        if not isinstance(schema, dict):
            raise ValueError(f"Tool {name!r}: schema must be a dict, got {type(schema).__name__}")
        params = schema.get("parameters")
        if params is not None and not isinstance(params, dict):
            raise ValueError(
                f"Tool {name!r}: schema['parameters'] must be an object (JSON Schema dict), "
                f"got {type(params).__name__}"
            )
        with self._lock:
            existing = self.tools.get(name)
            if existing and existing["toolset"] != toolset and not override:
                self._warn(f"Tool registration REJECTED: '{name}' (toolset '{toolset}') would "
                           f"shadow existing tool from toolset '{existing['toolset']}'")
                return None
            entry = dict(
                name=name, toolset=toolset, schema=schema, handler=handler, check_fn=check_fn,
                requires_env=requires_env, is_async=is_async, description=description,
                emoji=emoji, override=override,
            )
            self.tools[name] = entry

        def undo() -> None:
            with self._lock:
                if self.tools.get(name) is entry:
                    del self.tools[name]

        return self._handle("tool", name, undo)

    def _track_callback(
        self, kind: str, key: str, callback: Callable, mapping: dict[str, list[Callable]],
        valid: frozenset[str],
    ) -> FakeRegistration:
        if key not in valid:
            self._warn(f"Plugin '{self.plugin_id}' registered unknown {kind} '{key}' "
                       f"(valid: {', '.join(sorted(valid))})")
        with self._lock:
            mapping.setdefault(key, []).append(callback)

        def undo() -> None:
            with self._lock:
                callbacks = mapping.get(key, [])
                if callback in callbacks:
                    callbacks.remove(callback)
                if not callbacks:
                    mapping.pop(key, None)

        return self._handle(kind, key, undo)

    def register_hook(
        self, hook_name: str, callback: Callable, *, hold_stream: bool = False
    ) -> FakeRegistration:
        """Register a hook; ``hold_stream`` only counts on ``transform_llm_output``."""
        handle = self._track_callback("hook", hook_name, callback, self.hooks, self.host.hooks)
        if hold_stream and hook_name != "transform_llm_output":
            self._warn(f"Plugin '{self.plugin_id}' set hold_stream on '{hook_name}'; "
                       "only transform_llm_output rewrites the reply, ignored")
        else:
            handle.hold_stream = hold_stream
        return handle

    def register_middleware(self, kind: str, callback: Callable) -> FakeRegistration:
        return self._track_callback(
            "middleware", kind, callback, self.middleware, self.host.middleware
        )

    def register_command(
        self,
        name: str,
        handler: Callable,
        description: str = "",
        args_hint: str = "",
        argument_mode: str | None = None,
    ) -> FakeRegistration | None:
        clean = name.lower().strip().lstrip("/").replace(" ", "-")
        if not clean:
            self._warn(f"Plugin '{self.plugin_id}' tried to register a command with an empty name.")
            return None
        if clean in self.host.commands:
            self._warn(f"Plugin '{self.plugin_id}' tried to register command '/{clean}' which "
                       "conflicts with a built-in command. Skipping.")
            return None
        hint = (args_hint or "").strip()
        entry = dict(
            handler=handler,
            description=description or "Plugin command",
            plugin=self.manifest.get("name"),
            plugin_key=self.plugin_id,
            args_hint=hint,
            argument_mode=argument_mode
            if argument_mode in {"options", "text", "mixed"}
            else ("text" if hint else None),
        )
        with self._lock:
            self.commands[clean] = entry

        def undo() -> None:
            with self._lock:
                if self.commands.get(clean) is entry:
                    del self.commands[clean]

        return self._handle("command", clean, undo)

    def register_cli_command(
        self,
        name: str,
        help: str,
        setup_fn: Callable,
        handler_fn: Callable | None = None,
        description: str = "",
    ) -> FakeRegistration:
        entry = dict(
            name=name, help=help, description=description, setup_fn=setup_fn,
            handler_fn=handler_fn, plugin=self.manifest.get("name"), plugin_key=self.plugin_id,
        )
        with self._lock:
            self.cli_commands[name] = entry
        return self._handle("cli_command", name, lambda: self.cli_commands.pop(name, None))

    def register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: dict[str, Any] | None = None,
        inherit_from: str | None = None,
    ) -> FakeRegistration:
        me = self.manifest.get("name", self.plugin_id)
        if not key or not isinstance(key, str):
            raise ValueError(f"Plugin '{me}' tried to register auxiliary task with invalid key "
                             f"{key!r}")
        if not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"Plugin '{me}' auxiliary task key {key!r} must contain only "
                             "alphanumeric characters and underscores")
        if key in self.host.aux_tasks:
            raise ValueError(f"Plugin '{me}' cannot register auxiliary task {key!r} - that key "
                             f"is reserved for a built-in task. Pick a plugin-namespaced key "
                             f"(e.g. '{me}_{key}').")
        if inherit_from is not None and (
            not isinstance(inherit_from, str)
            or inherit_from == key
            or (inherit_from not in self.host.aux_tasks and inherit_from not in self.aux_tasks)
        ):
            self._warn(f"Plugin '{me}' auxiliary task {key!r}: ignoring inherit_from="
                       f"{inherit_from!r}")
            inherit_from = None
        task_defaults = (
            dict(defaults or {})
            if inherit_from
            else {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": 60,
                  "extra_body": {}, **(defaults or {})}
        )
        entry = dict(
            key=key, display_name=display_name, description=description,
            defaults=task_defaults, inherit_from=inherit_from, plugin=self.plugin_id,
        )
        with self._lock:
            self.aux_tasks[key] = entry
        return self._handle("auxiliary_task", key, lambda: self.aux_tasks.pop(key, None))

    def register_platform_handler(self, platform: str, factory: Callable) -> None:
        me = self.manifest.get("name", self.plugin_id)
        if not callable(factory):
            raise ValueError(f"Plugin '{me}' tried to register a platform handler factory with "
                             "a non-callable factory.")
        key = (platform or "").strip().lower()
        if not key:
            raise ValueError(f"Plugin '{me}' tried to register a platform handler factory with "
                             "an empty platform name.")
        with self._lock:
            self.platform_handlers.setdefault(key, []).append(factory)

    def register_telegram_handler(self, factory: Callable) -> None:
        self.register_platform_handler("telegram", factory)

    def registered_capabilities(self) -> dict[str, list[str]]:
        """Names in the same categories ``hermes plugins validate`` diffs against the manifest."""
        with self._lock:
            return {
                "tools": sorted(self.tools),
                "hooks": sorted(k for k, v in self.hooks.items() if v),
                "middleware": sorted(k for k, v in self.middleware.items() if v),
            }

    # -- messaging
    def inject_message(
        self,
        content: str,
        role: str = "user",
        *,
        session_key: str | None = None,
        origin: Mapping[str, Any] | None = None,
    ) -> bool:
        reason = self._injection_refusal(session_key, origin)
        with self._lock:
            if reason:  # refused by the host before reaching any session: not "injected"
                self.inject_refused.append((reason, content))
            else:
                self.injected.append(InjectedMessage(content, role, session_key, origin))
        if reason:
            self._warn(f"inject_message: {reason}")
            return False
        return self.inject_result

    def _injection_refusal(self, session_key: str | None, origin: Mapping[str, Any] | None) -> str:
        """Why the real ``hermes_cli.plugins_injection.inject_plugin_message`` would return
        ``False`` before reaching a session ('' = accepted). Mirrors its checks in order."""
        if self.classic_cli and origin is None:
            return ""  # the in-process REPL takes it, whatever the key is
        if session_key and origin is not None:
            return "pass session_key or origin, not both"
        if not session_key and origin is None and not self.gateway_live:
            return "gateway mode requires an existing session_key or an origin"
        if origin is not None and not isinstance(origin, Mapping):
            return "origin must be a mapping in the SessionSource.to_dict() shape"
        if not self.gateway_injection_allowed:
            return (f"gateway injection denied for plugin {self._plugin_id}; set plugins.entries."
                    f"{self._plugin_id}.allow_gateway_injection: true to allow it")
        if not self.gateway_live:
            return "no live gateway is available"
        return ""

    def post_message(self, text: str) -> FakePostHandle:
        """Never raises; with ``post_ok=False`` returns ``ok=False`` like a call with no session."""
        handle = (
            FakePostHandle(text=text)
            if self.post_ok
            else FakePostHandle(text=text, ok=False, error="no_conversation")
        )
        with self._lock:
            self.posted.append(handle)
        return handle

    async def apost_message(self, text: str) -> FakePostHandle:
        return self.post_message(text)

    def ask_choice(self, question: str, choices: list) -> str | None:
        """Pop the next scripted answer from ``choice_answers``; ``None`` (unanswerable, as under
        cron/gateway) when none is scripted or the answer is not one of the choice values."""
        values = [c.get("value") if isinstance(c, Mapping) else c for c in (choices or [])]
        with self._lock:
            self.choices_asked.append((question, list(choices or [])))
            answer = self.choice_answers.popleft() if self.choice_answers else None
        return answer if answer in values else None

    # -- async tasks
    def spawn_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task:
        """Like the host: needs a running loop (``RuntimeError`` otherwise); tracked until done."""
        if not asyncio.iscoroutine(coro):
            raise TypeError("spawn_task expects a coroutine")
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro, name=name or f"plugin:{self._plugin_id}:task")
        with self._lock:
            self.tasks = [t for t in self.tasks if not t.done()]
            self.tasks.append(task)
        return task

    def _controlled_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._owns_loop = True
        return self._loop

    def run(self, awaitable: Any, timeout: float | None = 5.0) -> Any:
        """Run ``awaitable`` to completion on the controlled loop (spawned tasks advance too)."""
        loop = self._controlled_loop()
        return loop.run_until_complete(asyncio.wait_for(awaitable, timeout))

    def call_in_loop(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call sync ``fn`` from inside the controlled loop, as the gateway would call a handler."""

        async def _inside() -> Any:
            return fn(*args, **kwargs)

        return self.run(_inside())

    def run_until_idle(self, max_iterations: int = 100) -> None:
        """Step the controlled loop until no task spawned on it can progress (or limit reached)."""
        loop = self._controlled_loop()
        for _ in range(max_iterations):
            pending = [t for t in self.tasks if not t.done() and t.get_loop() is loop]
            if not pending:
                break
            loop.run_until_complete(asyncio.sleep(0))

    # -- unload
    def on_unload(self, callback: Callable[[], None]) -> FakeRegistration:
        if not callable(callback):
            raise TypeError("on_unload callback must be callable")
        with self._lock:
            self._unload_callbacks.append(callback)
        name = getattr(callback, "__name__", "callback")

        def undo() -> None:
            with self._lock:
                if callback in self._unload_callbacks:
                    self._unload_callbacks.remove(callback)

        return self._handle("on_unload", name, undo)

    def unload(self) -> None:
        """Run on_unload callbacks (reverse order; errors logged and kept), cancel spawned tasks."""
        with self._lock:
            callbacks = list(reversed(self._unload_callbacks))
            self._unload_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:  # Hermes logs, never propagates
                logger.warning("on_unload callback failed: %s", exc)
                self.unload_errors.append(exc)
        for task in self.tasks:
            if not task.done():
                task.cancel()
        loop = self._loop
        if loop is not None and not loop.is_closed() and not loop.is_running():
            ours = [t for t in self.tasks if t.get_loop() is loop and not t.done()]
            if ours:
                loop.run_until_complete(asyncio.gather(*ours, return_exceptions=True))
        self.unloaded = True

    def close(self) -> None:
        """Unload (if needed) and release the controlled loop (the fixture calls this)."""
        if not self.unloaded:
            self.unload()
        if self._owns_loop and self._loop is not None and not self._loop.is_closed():
            self._loop.close()

    # -- convenience for tests
    def run_command(self, name: str, raw_args: str = "", **kwargs: Any) -> Any:
        """Run a registered slash command; ``kwargs`` are the host's session keywords
        (``session_id``, ``session_key``, ``platform``, ``source``)."""
        return self.commands[name]["handler"](raw_args, **kwargs)

    def call_tool(self, name: str, args: dict, **kwargs: Any) -> Any:
        return self.tools[name]["handler"](args, **kwargs)

    def dispatch_tool(self, tool_name: str, args: dict, **kwargs: Any) -> str:
        """Dispatch to a tool this ctx registered; errors become ``{"error": ...}`` as in Hermes."""
        entry = self.tools.get(tool_name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            return entry["handler"](args, **kwargs)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
