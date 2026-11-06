"""The Hermes host surface the fakes validate against, read from the real Hermes source.

``FakeCtx`` must reject what the real ``PluginContext`` rejects: unknown hook and middleware
names, slash commands that clash with built-ins, tool names that shadow built-in tools,
reserved auxiliary-task keys. Those vocabularies live in the Hermes checkout:

* ``VALID_HOOKS``                 ``hermes_cli/plugins.py``
* ``VALID_MIDDLEWARE``            ``hermes_cli/middleware.py`` (a set of named constants)
* built-in slash commands         ``hermes_cli/commands.py`` (``COMMAND_REGISTRY`` names + aliases)
* built-in auxiliary tasks        ``hermes_cli/main_provider_setup.py`` (``_AUX_TASKS``)
* built-in tools                  ``tools/*.py`` (``registry.register(name=...)`` calls)

The files are parsed with :mod:`ast` (never imported or executed, never written). The checkout
is ``$HERMES_AGENT_DIR`` or ``~/.hermes/hermes-agent``. When it is missing (CI), the pinned
``SNAPSHOT`` below is used instead; ``tests/test_hermes_surface.py`` fails when the snapshot
drifts from a present checkout. Refresh it with::

    uv run --group dev python hermes-sentinel/tests/harness/hermes_surface.py
"""

from __future__ import annotations

import ast
import functools
import os
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SNAPSHOT",
    "HermesSurface",
    "hermes_agent_dir",
    "load_surface",
    "parse_surface",
    "surface",
]


@dataclass(frozen=True)
class HermesSurface:
    """Name sets the host validates plugin registrations against."""

    hooks: frozenset[str]
    middleware: frozenset[str]
    commands: frozenset[str]
    aux_tasks: frozenset[str]
    tools: frozenset[str]
    source: str = "snapshot"

    def same_names(self, other: HermesSurface) -> bool:
        """True when every name set matches (``source`` is ignored)."""
        return all(
            getattr(self, f) == getattr(other, f)
            for f in ("hooks", "middleware", "commands", "aux_tasks", "tools")
        )


def hermes_agent_dir() -> Path:
    """Location of the read-only Hermes checkout (may not exist)."""
    # Not a credential: a filesystem location for a read-only source parse.
    configured = os.environ.get("HERMES_AGENT_DIR")  # rule-lint: ignore[11] path, not a secret
    return Path(configured).expanduser() if configured else Path.home() / ".hermes/hermes-agent"


# ----------------------------------------------------------------------------- parsing


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_assignment(tree: ast.Module, name: str) -> ast.expr | None:
    """Value of a top-level ``name = ...`` or ``name: T = ...``."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return node.value
    return None


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Top-level ``NAME = "literal"`` bindings (to resolve constants used inside a set)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _string_set(value: ast.expr | None, constants: dict[str, str], what: str) -> frozenset[str]:
    if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        raise ValueError(f"{what}: expected a literal set/list")
    names: set[str] = set()
    for elt in value.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            names.add(elt.value)
        elif isinstance(elt, ast.Name) and elt.id in constants:
            names.add(constants[elt.id])
        else:
            raise ValueError(f"{what}: unsupported element {ast.dump(elt)}")
    if not names:
        raise ValueError(f"{what}: empty")
    return frozenset(names)


def _parse_hooks(root: Path) -> frozenset[str]:
    tree = _parse(root / "hermes_cli" / "plugins.py")
    return _string_set(_module_assignment(tree, "VALID_HOOKS"), {}, "VALID_HOOKS")


def _parse_middleware(root: Path) -> frozenset[str]:
    tree = _parse(root / "hermes_cli" / "middleware.py")
    return _string_set(
        _module_assignment(tree, "VALID_MIDDLEWARE"), _string_constants(tree), "VALID_MIDDLEWARE"
    )


def _parse_commands(root: Path) -> frozenset[str]:
    tree = _parse(root / "hermes_cli" / "commands.py")
    registry = _module_assignment(tree, "COMMAND_REGISTRY")
    if not isinstance(registry, ast.List):
        raise ValueError("COMMAND_REGISTRY: expected a literal list")
    names: set[str] = set()
    for call in registry.elts:
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "CommandDef"):
            continue
        if call.args and isinstance(call.args[0], ast.Constant):
            names.add(str(call.args[0].value))
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                names.add(str(kw.value.value))
            if kw.arg == "aliases" and isinstance(kw.value, (ast.Tuple, ast.List)):
                names.update(
                    str(e.value) for e in kw.value.elts if isinstance(e, ast.Constant)
                )
    if not names:
        raise ValueError("COMMAND_REGISTRY: no CommandDef entries found")
    return frozenset(n.lower() for n in names)


def _parse_aux_tasks(root: Path) -> frozenset[str]:
    tree = _parse(root / "hermes_cli" / "main_provider_setup.py")
    value = _module_assignment(tree, "_AUX_TASKS")
    if not isinstance(value, ast.List):
        raise ValueError("_AUX_TASKS: expected a literal list")
    keys = {
        str(t.elts[0].value)
        for t in value.elts
        if isinstance(t, ast.Tuple) and t.elts and isinstance(t.elts[0], ast.Constant)
    }
    if not keys:
        raise ValueError("_AUX_TASKS: empty")
    return frozenset(keys)


def _parse_tools(root: Path) -> frozenset[str]:
    names: set[str] = set()
    for path in sorted((root / "tools").rglob("*.py")):
        try:
            tree = _parse(path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "registry"
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    names.add(kw.value.value)
    if not names:
        raise ValueError("tools/: no registry.register(name=...) calls found")
    return frozenset(names)


def parse_surface(root: Path) -> HermesSurface:
    """Parse the vocabularies from a Hermes checkout. Raises ``OSError``/``ValueError``."""
    return HermesSurface(
        hooks=_parse_hooks(root),
        middleware=_parse_middleware(root),
        commands=_parse_commands(root),
        aux_tasks=_parse_aux_tasks(root),
        tools=_parse_tools(root),
        source=str(root),
    )


_LOCK = threading.Lock()


@functools.lru_cache(maxsize=4)
def _cached_parse(root: str) -> HermesSurface:
    return parse_surface(Path(root))


def load_surface(root: Path | None = None) -> HermesSurface:
    """The real surface when the checkout is readable, else :data:`SNAPSHOT`."""
    root = root or hermes_agent_dir()
    if not (root / "hermes_cli" / "plugins.py").is_file():
        return SNAPSHOT
    with _LOCK:
        try:
            return _cached_parse(str(root.resolve()))
        except (OSError, ValueError, SyntaxError):
            return SNAPSHOT


def surface() -> HermesSurface:
    """Shorthand for :func:`load_surface` with the default checkout."""
    return load_surface()


# ----------------------------------------------------------------------------- snapshot


def _fs(names: Iterable[str]) -> frozenset[str]:
    return frozenset(names)


# Pinned from hermes-agent (requires_hermes floor line). Regenerate, do not hand-edit.
# fmt: off
SNAPSHOT = HermesSurface(
    hooks=_fs((
        'agent_loop_stopped',
        'api_request_error',
        'gateway_platform_event',
        'kanban_task_blocked',
        'kanban_task_claimed',
        'kanban_task_completed',
        'on_human_input_request',
        'on_human_input_resolved',
        'on_interim_message',
        'on_kanban_dispatch_tick',
        'on_kanban_task_updated',
        'on_kanban_worker_exited',
        'on_kanban_worker_spawned',
        'on_kanban_worker_stale_claim',
        'on_room_member_activity',
        'on_session_delete',
        'on_session_end',
        'on_session_finalize',
        'on_session_reset',
        'on_session_start',
        'on_skill_lifecycle',
        'on_stream_delta',
        'on_stream_end',
        'on_stream_start',
        'post_api_request',
        'post_approval_response',
        'post_auxiliary_call',
        'post_compaction',
        'post_gateway_admission',
        'post_llm_call',
        'post_tool_call',
        'pre_api_request',
        'pre_approval_request',
        'pre_auxiliary_call',
        'pre_command',
        'pre_gateway_dispatch',
        'pre_llm_call',
        'pre_tool_call',
        'pre_transcription',
        'pre_verify',
        'subagent_start',
        'subagent_stop',
        'transform_api_error_classification',
        'transform_llm_output',
        'transform_terminal_output',
        'transform_tool_result',
    )),
    middleware=_fs((
        'llm_execution',
        'llm_request',
        'tool_execution',
        'tool_request',
    )),
    commands=_fs((
        'agents',
        'approvals',
        'approve',
        'autoprune',
        'battery',
        'bg',
        'blueprint',
        'bp',
        'branch',
        'browser',
        'btw',
        'bundles',
        'busy',
        'clear',
        'codex-runtime',
        'codex_runtime',
        'commands',
        'compact',
        'compose',
        'compress',
        'config',
        'context',
        'copy',
        'cron',
        'ctx',
        'curator',
        'debug',
        'deny',
        'diff',
        'discard',
        'egress',
        'exit',
        'export',
        'fast',
        'focus',
        'footer',
        'fork',
        'gateway',
        'generate-pet',
        'goal',
        'handoff',
        'hatch',
        'hb',
        'heartbeat',
        'help',
        'history',
        'image',
        'import',
        'indicator',
        'init',
        'insights',
        'journey',
        'kanban',
        'learn',
        'learning',
        'login',
        'loop',
        'memory',
        'memory-graph',
        'moa',
        'model',
        'new',
        'palette',
        'paste',
        'pause',
        'personality',
        'pet',
        'plan',
        'platform',
        'platforms',
        'plugins',
        'proactive',
        'profile',
        'prompt',
        'q',
        'queue',
        'quit',
        'reasoning',
        'redraw',
        'refine',
        'reload',
        'reload-mcp',
        'reload-skills',
        'reload_mcp',
        'reload_skills',
        'reset',
        'restart',
        'resume',
        'retry',
        'review',
        'rollback',
        's',
        'save',
        'sb',
        'sessions',
        'set-home',
        'sethome',
        'skills',
        'skin',
        'snap',
        'snapshot',
        'start',
        'status',
        'statusbar',
        'steer',
        'stop',
        'subgoal',
        'subscription',
        'suggest',
        'suggestions',
        'tasks',
        'timestamps',
        'title',
        'tools',
        'toolsets',
        'topic',
        'topup',
        'ts',
        'undo',
        'update',
        'upgrade',
        'usage',
        'v',
        'verbose',
        'version',
        'voice',
        'wake',
        'whoami',
        'worktree',
        'yolo',
    )),
    aux_tasks=_fs((
        'approval',
        'compression',
        'curator',
        'kanban_decomposer',
        'mcp',
        'memory_query_rewrite',
        'profile_describer',
        'review',
        'skills_hub',
        'title_generation',
        'triage_specifier',
        'tts_audio_tags',
        'vision',
        'voice_chat',
    )),
    tools=_fs((
        'annotate_preview',
        'browser_cdp',
        'browser_dialog',
        'browser_exec',
        'browser_vault',
        'clarify',
        'close_terminal',
        'computer_use',
        'cronjob_manage',
        'delegate_task',
        'desktop_preview',
        'desktop_project',
        'drive_preview',
        'execute_code',
        'feishu_doc_read',
        'focus_pane',
        'gui_tour',
        'image_generate',
        'manage_catalog',
        'manage_connections',
        'memory',
        'patch',
        'process_manage',
        'react_to_message',
        'read_file',
        'read_terminal',
        'read_window_below',
        'search_files',
        'session_search',
        'set_theme',
        'setup_beat',
        'show_tip',
        'skill_manage',
        'skill_view',
        'skills_list',
        'terminal',
        'text_to_speech',
        'todo_list',
        'video_analyze',
        'video_generate',
        'vision_analyze',
        'web_extract',
        'web_search',
        'write_file',
        'x_search',
    )),
    source="snapshot",
)
# fmt: on


def _render(names: frozenset[str]) -> str:
    return "(\n" + "".join(f"        {n!r},\n" for n in sorted(names)) + "    )"


def main(argv: list[str] | None = None) -> int:
    """Print the snapshot block for the current checkout (paste it over ``SNAPSHOT``)."""
    root = Path(argv[0]) if argv else hermes_agent_dir()
    parsed = parse_surface(root)
    for field, value in (
        ("hooks", parsed.hooks),
        ("middleware", parsed.middleware),
        ("commands", parsed.commands),
        ("aux_tasks", parsed.aux_tasks),
        ("tools", parsed.tools),
    ):
        print(f"    {field}=_fs({_render(value)}),")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
