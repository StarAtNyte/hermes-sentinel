"""Tests for scripts/structure_lint.py: a bad and a good fixture for every rule."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "structure_lint", REPO_ROOT / "scripts" / "structure_lint.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["structure_lint"] = lint
_SPEC.loader.exec_module(lint)

DOC = '"""Doc."""\n'


def make_plugin(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create ``tmp_path/plugin`` containing ``files`` (relative path -> source)."""
    root = tmp_path / "plugin"
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def rules(root: Path, **limits) -> list[str]:
    """Rule ids reported for the tree."""
    return [f.rule for f in lint.lint_tree(root, lint.Limits(**limits))]


def test_module_length(tmp_path):
    body = DOC + "x = 1\n" * 10
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": body})
    assert "module-length" in rules(root, module_lines=5)
    assert "module-length" not in rules(root, module_lines=50)


def test_class_length(tmp_path):
    src = DOC + 'class A:\n    """Doc."""\n' + "    x = 1\n" * 10
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": src})
    assert "class-length" in rules(root, class_lines=5)
    assert "class-length" not in rules(root, class_lines=50)


def test_function_length_warning_then_error(tmp_path):
    src = DOC + 'def f():\n    """Doc."""\n' + "    x = 1\n" * 10
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": src})
    warn = lint.lint_tree(root, lint.Limits(function_lines=5, function_hard=50))
    assert [(f.rule, f.severity) for f in warn] == [("function-length", "warning")]
    hard = lint.lint_tree(root, lint.Limits(function_lines=5, function_hard=8))
    assert [(f.rule, f.severity) for f in hard] == [("function-length", "error")]
    assert rules(root) == []


def test_nesting_depth(tmp_path):
    bad = DOC + (
        'def f(a):\n    """Doc."""\n'
        "    if a:\n        for i in a:\n            while i:\n                if i:\n"
        "                    return 1\n    return 0\n"
    )
    good = DOC + (
        'def f(a):\n    """Doc."""\n'
        "    if a:\n        for i in a:\n            if i:\n"
        "                return 1\n    return 0\n"
    )
    assert "nesting-depth" in rules(
        make_plugin(tmp_path / "bad", {"__init__.py": DOC, "m.py": bad})
    )
    assert "nesting-depth" not in rules(
        make_plugin(tmp_path / "good", {"__init__.py": DOC, "m.py": good})
    )


def test_nested_function_does_not_inflate_outer_depth(tmp_path):
    src = DOC + (
        'def f(a):\n    """Doc."""\n    if a:\n        for i in a:\n'
        "            def inner(b):\n                if b:\n                    if b:\n"
        "                        return 1\n                return 0\n"
        "            inner(i)\n"
    )
    found = lint.lint_tree(make_plugin(tmp_path, {"__init__.py": DOC, "m.py": src}), lint.Limits())
    assert not [f for f in found if f.rule == "nesting-depth" and "f()" in f.message]


@pytest.mark.parametrize(
    "body",
    [
        "import os\nfoo()\n",
        "def helper():\n    return 1\n",
        "class A:\n    pass\n",
    ],
)
def test_init_logic_bad(tmp_path, body):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "pkg/__init__.py": DOC + body})
    found = [f for f in lint.lint_tree(root, lint.Limits()) if f.rule == "init-logic"]
    assert found and found[0].path.endswith("pkg/__init__.py")


def test_init_logic_good(tmp_path):
    body = DOC + 'from .a import B\n__all__ = ["B"]\n__version__ = "1.0"\nLIMIT = 3\n'
    root = make_plugin(
        tmp_path,
        {"__init__.py": DOC, "pkg/__init__.py": body, "pkg/a.py": DOC + "class B:\n    '''D.'''\n"},
    )
    assert "init-logic" not in rules(root)


def test_root_init_allows_register_and_called_helpers_only(tmp_path):
    good = DOC + (
        "from .a import go\n\n"
        'def _setup(ctx):\n    """Doc."""\n    go(ctx)\n\n'
        'def register(ctx):\n    """Doc."""\n    _setup(ctx)\n'
    )
    bad = good + 'def unrelated():\n    """Doc."""\n    return 1\n'
    assert "init-logic" not in rules(
        make_plugin(tmp_path / "g", {"__init__.py": good, "a.py": DOC})
    )
    found = lint.lint_tree(
        make_plugin(tmp_path / "b", {"__init__.py": bad, "a.py": DOC}), lint.Limits()
    )
    assert [f.rule for f in found if f.rule == "init-logic"] == ["init-logic"]


def test_flat_package(tmp_path):
    files = {"__init__.py": DOC} | {f"m{i}.py": DOC for i in range(9)}
    assert "flat-package" in rules(make_plugin(tmp_path / "bad", files))
    files = {"__init__.py": DOC} | {f"m{i}.py": DOC for i in range(8)}
    assert "flat-package" not in rules(make_plugin(tmp_path / "good", files))
    assert "flat-package" not in rules(
        make_plugin(tmp_path / "ovr", {"__init__.py": DOC} | {f"m{i}.py": DOC for i in range(9)}),
        package_modules=9,
    )


def test_missing_docstrings(tmp_path):
    bad = "class A:\n    def run(self):\n        return 1\n\ndef go():\n    return 1\n"
    found = lint.lint_tree(make_plugin(tmp_path, {"__init__.py": DOC, "m.py": bad}), lint.Limits())
    messages = " ".join(f.message for f in found if f.rule == "missing-docstring")
    for needle in ("module", "class A", "method run", "function go"):
        assert needle in messages


def test_docstrings_private_and_dunder_exempt(tmp_path):
    good = DOC + (
        'class A:\n    """Doc."""\n\n    def __repr__(self):\n        return "a"\n\n'
        "    def _hidden(self):\n        return 1\n\n"
        '    def __init__(self):\n        """Doc."""\n\n'
        "def _private():\n    return 1\n\n"
        "class _Internal:\n    def run(self):\n        return 1\n"
    )
    assert "missing-docstring" not in rules(
        make_plugin(tmp_path, {"__init__.py": DOC, "m.py": good})
    )


def test_bare_except_and_except_pass(tmp_path):
    bad = DOC + (
        'def f():\n    """Doc."""\n    try:\n        return 1\n    except:\n        return 2\n\n'
        'def g():\n    """Doc."""\n    try:\n        return 1\n'
        "    except Exception:\n        pass\n"
    )
    got = rules(make_plugin(tmp_path / "b", {"__init__.py": DOC, "m.py": bad}))
    assert "bare-except" in got and "except-pass" in got
    good = DOC + (
        'def f():\n    """Doc."""\n    try:\n        return 1\n'
        "    except ValueError:\n        pass\n"
        "    except Exception as error:\n        return str(error)\n"
    )
    got = rules(make_plugin(tmp_path / "g", {"__init__.py": DOC, "m.py": good}))
    assert "bare-except" not in got and "except-pass" not in got


@pytest.mark.parametrize(
    "comment", ["x = compute(1)", "foo.bar(3)", "import os", "return value", "if x:"]
)
def test_commented_code_flagged(tmp_path, comment):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + f"# {comment}\n"})
    assert "commented-code" in rules(root)


@pytest.mark.parametrize(
    "comment",
    [
        "Holds the lock because the thread reads this",
        "return the value",
        "Note: see below",
        "noqa: E501",
        "x",
    ],
)
def test_prose_comments_pass(tmp_path, comment):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + f"# {comment}\n"})
    assert "commented-code" not in rules(root)


@pytest.mark.parametrize("comment", ["TODO: fix", "FIXME later"])
def test_todo_bad(tmp_path, comment):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + f"# {comment}\n"})
    assert "todo-issue" in rules(root)


@pytest.mark.parametrize(
    "comment", ["TODO(issue 12): fix", "TODO #45 fix", "FIXME 2026-11-20 revisit"]
)
def test_todo_good(tmp_path, comment):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + f"# {comment}\n"})
    assert "todo-issue" not in rules(root)


def test_ignore_needs_reason(tmp_path):
    src = DOC + "x = 1\n" * 3
    with_reason = src.replace(
        "x = 1\n", "x = 1  # structure-lint: ignore[module-length] generated\n", 0
    )
    # Module findings attach to line 1, so the ignore goes on the docstring line.
    ok = '"""Doc."""  # structure-lint: ignore[module-length] generated table\n' + "x = 1\n" * 10
    bare = '"""Doc."""  # structure-lint: ignore[module-length]\n' + "x = 1\n" * 10
    assert with_reason == src
    assert "module-length" not in rules(
        make_plugin(tmp_path / "ok", {"__init__.py": DOC, "m.py": ok}), module_lines=5
    )
    found = lint.lint_tree(
        make_plugin(tmp_path / "bare", {"__init__.py": DOC, "m.py": bare}),
        lint.Limits(module_lines=5),
    )
    assert [f for f in found if f.rule == "module-length" and "reason is required" in f.message]


def test_ignore_only_suppresses_named_rule(tmp_path):
    src = "def f():  # structure-lint: ignore[nesting-depth] fine\n    return 1\n"
    assert "missing-docstring" in rules(
        make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + src})
    )


def test_tests_and_pycache_skipped(tmp_path):
    root = make_plugin(
        tmp_path,
        {
            "__init__.py": DOC,
            "tests/test_a.py": "def f():\n    pass\n",
            "__pycache__/x.py": "def f(:\n",
        },
    )
    assert rules(root) == []


def test_syntax_error_reported(tmp_path):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": "def f(:\n"})
    assert "syntax-error" in rules(root)


def test_clean_tree(tmp_path):
    root = make_plugin(
        tmp_path, {"__init__.py": DOC, "a.py": DOC + 'def f():\n    """Doc."""\n    return 1\n'}
    )
    assert lint.lint_tree(root, lint.Limits()) == []


def bad_tree(tmp_path: Path) -> Path:
    """A tree with exactly one error (missing docstring)."""
    return make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + "def go():\n    return 1\n"})


def test_cli_exit_codes(tmp_path, capsys):
    assert lint.main([str(bad_tree(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "m.py:2: [missing-docstring]" in out
    clean = make_plugin(tmp_path / "c", {"__init__.py": DOC})
    assert lint.main([str(clean)]) == 0
    assert lint.main([str(tmp_path / "missing")]) == 2


def test_warnings_do_not_fail(tmp_path):
    src = DOC + 'def f():\n    """Doc."""\n' + "    x = 1\n" * 10
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": src})
    assert lint.main([str(root), "--max-function-lines", "5"]) == 0
    assert lint.main([str(root), "--max-function-lines", "5", "--max-function-hard", "8"]) == 1


def test_report_exits_zero_and_lists_offenders(tmp_path, capsys):
    assert lint.main([str(bad_tree(tmp_path)), "--report"]) == 0
    out = capsys.readouterr().out
    assert "missing-docstring" in out and "Top offenders" in out


def test_json_output(tmp_path, capsys):
    lint.main([str(bad_tree(tmp_path)), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["rule"] == "missing-docstring" and data[0]["line"] == 2


def test_max_overrides_via_cli(tmp_path):
    root = make_plugin(tmp_path, {"__init__.py": DOC, "m.py": DOC + "x = 1\n" * 10})
    assert lint.main([str(root)]) == 0
    assert lint.main([str(root), "--max-module-lines", "5"]) == 1
