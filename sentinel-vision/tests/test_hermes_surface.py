"""The host vocabularies (hooks, middleware, commands, aux tasks, tools) the fakes validate with."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import hermes_surface
from harness.hermes_surface import SNAPSHOT, hermes_agent_dir, load_surface, parse_surface


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mini_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    _write(
        root,
        "hermes_cli/plugins.py",
        'VALID_HOOKS: set[str] = {"pre_tool_call", "post_tool_call"}\n',
    )
    _write(
        root,
        "hermes_cli/middleware.py",
        'A = "tool_request"\nB = "llm_request"\nVALID_MIDDLEWARE: set[str] = {A, B}\n',
    )
    _write(
        root,
        "hermes_cli/commands.py",
        "COMMAND_REGISTRY: list = [\n"
        '    CommandDef("help", "Help", "Info", aliases=("h", "?")),\n'
        '    CommandDef(name="New", description="d", category="Session"),\n'
        "]\n",
    )
    _write(
        root,
        "hermes_cli/main_provider_setup.py",
        '_AUX_TASKS = [("vision", "Vision", "v"), ("compression", "C", "c")]\n',
    )
    _write(
        root,
        "tools/file_tools.py",
        'registry.register(name="read_file", toolset="file", schema={}, handler=None)\n'
        'other.register(name="not_a_tool")\n',
    )
    _write(root, "tools/broken.py", "def (:\n")
    return root


def test_parse_mini_checkout(mini_checkout: Path):
    parsed = parse_surface(mini_checkout)
    assert parsed.hooks == {"pre_tool_call", "post_tool_call"}
    assert parsed.middleware == {"tool_request", "llm_request"}
    assert parsed.commands == {"help", "h", "?", "new"}
    assert parsed.aux_tasks == {"vision", "compression"}
    assert parsed.tools == {"read_file"}
    assert parsed.source == str(mini_checkout)


def test_parse_rejects_unexpected_shapes(mini_checkout: Path):
    _write(mini_checkout, "hermes_cli/plugins.py", "VALID_HOOKS = make_hooks()\n")
    with pytest.raises(ValueError, match="VALID_HOOKS"):
        parse_surface(mini_checkout)


def test_load_falls_back_to_snapshot(tmp_path: Path, mini_checkout: Path):
    assert load_surface(tmp_path / "missing") is SNAPSHOT
    _write(mini_checkout, "hermes_cli/middleware.py", "VALID_MIDDLEWARE = {UNKNOWN}\n")
    assert load_surface(mini_checkout) is SNAPSHOT


def test_hermes_agent_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path))
    assert hermes_agent_dir() == tmp_path


def test_snapshot_sanity():
    assert {"pre_tool_call", "post_tool_call", "on_session_start"} <= SNAPSHOT.hooks
    assert SNAPSHOT.middleware == {"tool_request", "tool_execution", "llm_request", "llm_execution"}
    assert {"help", "new", "model"} <= SNAPSHOT.commands
    assert "sentinel" not in SNAPSHOT.commands  # our slash command must stay free
    assert "vision" in SNAPSHOT.aux_tasks and "sentinel_vision" not in SNAPSHOT.aux_tasks
    assert {"read_file", "write_file"} <= SNAPSHOT.tools


def test_snapshot_matches_hermes_checkout():
    root = hermes_agent_dir()
    if not (root / "hermes_cli" / "plugins.py").is_file():
        pytest.skip(f"no Hermes checkout at {root}")
    live = parse_surface(root)
    for field in ("hooks", "middleware", "commands", "aux_tasks", "tools"):
        added = sorted(getattr(live, field) - getattr(SNAPSHOT, field))
        removed = sorted(getattr(SNAPSHOT, field) - getattr(live, field))
        assert not added and not removed, (
            f"Hermes {field} changed (added {added}, removed {removed}); regenerate SNAPSHOT: "
            "uv run --group dev python sentinel-vision/tests/harness/hermes_surface.py"
        )


def test_main_prints_snapshot_block(mini_checkout: Path, capsys: pytest.CaptureFixture[str]):
    assert hermes_surface.main([str(mini_checkout)]) == 0
    out = capsys.readouterr().out
    assert "hooks=_fs((" in out and "'pre_tool_call'," in out
