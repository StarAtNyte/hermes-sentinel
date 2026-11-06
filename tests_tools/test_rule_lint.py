"""Tests for scripts/rule_lint.py: every bad sample must be flagged with the right rule,
every good sample must pass clean."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("rule_lint", REPO_ROOT / "scripts" / "rule_lint.py")
assert _SPEC is not None and _SPEC.loader is not None
rule_lint = importlib.util.module_from_spec(_SPEC)
sys.modules["rule_lint"] = rule_lint
_SPEC.loader.exec_module(rule_lint)


def _lint(tmp_path: Path, name: str, content: str | bytes) -> list:
    plugin = tmp_path / "plugin"
    target = plugin / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return rule_lint.lint_paths(plugin, all_files=True)


def _rules(findings: list) -> set[int]:
    return {f.rule for f in findings}


# ---- Python: bad samples --------------------------------------------------------

PY_BAD = [
    # rule 11: environment and credential files
    ("import os\nkey = os.environ['OPENAI_API_KEY']\n", 11),
    ("import os\nkey = os.environ.get('X')\n", 11),
    ("import os\nkey = os.getenv('X')\n", 11),
    ("from os import environ\nkey = environ['X']\n", 11),
    ("from os import getenv\nkey = getenv('X')\n", 11),
    ("import os as o\nk = o.environ['X']\n", 11),
    ("from pathlib import Path\np = Path.home() / '.env'\n", 11),
    ("p = '/home/u/.hermes/auth.json'\n", 11),
    ("p = '~/.env.local'\n", 11),
    # rule 7: dynamic code
    ("eval('1+1')\n", 7),
    ("exec(open('x').read())\n", 7),
    ("code = compile('x=1', 'f', 'exec')\n", 7),
    # rule 9: sys.modules, monkeypatching, private internals
    ("import sys\nsys.modules['agent'] = object()\n", 9),
    ("import sys\ndel sys.modules['agent']\n", 9),
    ("import sys\nsys.modules.update({'x': 1})\n", 9),
    ("from run_agent import AIAgent\nAIAgent.run = lambda self: None\n", 9),
    ("import hermes_cli.plugins as hp\nhp.PluginManager.load = None\n", 9),
    ("from gateway import server\nsetattr(server, 'handle', None)\n", 9),
    ("from agent.plugin_llm import PluginLlm\nsetattr(PluginLlm, 'x', 1)\n", 9),
    ("from hermes_cli.plugins import _private_helper\n", 9),
    ("import hermes_cli._internal\n", 9),
    ("from hermes_state import db\ndb._execute_write('x')\n", 9),
    ("def register(ctx):\n    ctx._manager.tools.clear()\n", 9),
    ("import json\njson.dumps = None\n", None),  # monkeypatching stdlib module attr: not flagged
    ("from json import JSONEncoder\nJSONEncoder.default = None\n", 9),
    ("import builtins\nbuiltins.print = None\n", 9),
    ("def f(monkeypatch):\n    monkeypatch.setattr('a.b', 1)\n", 9),
    ("from unittest import mock\nmock.patch('agent.x').start()\n", 9),
    # rule 3: self-update
    ("URL = 'https://example.com/releases/plugin.py'\n", 3),
    ("URL = 'https://cdn.example.com/desktop/plugin.js?v=2'\n", 3),
    ("cmd = 'git pull origin main'\n", 3),
    ("import subprocess\nsubprocess.run('pip install -U hermes-sentinel', shell=True)\n", 3),
    (
        "import urllib.request\nfrom pathlib import Path\n"
        "data = urllib.request.urlopen(u).read()\n"
        "Path(__file__).write_bytes(data)\n",
        3,
    ),
    (
        "import requests, os\n"
        "body = requests.get(u).text\n"
        "with open(os.path.join(os.path.dirname(__file__), 'x.py'), 'w') as fh:\n"
        "    fh.write(body)\n",
        3,
    ),
]


@pytest.mark.parametrize(("source", "rule"), PY_BAD)
def test_python_bad_samples(tmp_path: Path, source: str, rule: int | None) -> None:
    findings = _lint(tmp_path, "mod.py", source)
    if rule is None:
        assert findings == []
    else:
        assert rule in _rules(findings), findings


# ---- Python: good samples -------------------------------------------------------

PY_GOOD = [
    "from agent.secret_scope import get_secret\nkey = get_secret('SENTINEL_KEY')\n",
    "from plugins.plugin_storage import plugin_data_dir\nd = plugin_data_dir('hermes-sentinel')\n",
    "from agent.plugin_llm import PluginLlmImageInput\nimg = PluginLlmImageInput(data=b'', "
    "mime_type='image/png')\n",
    "from .sentinel import gate\ngate.THRESHOLD = 3\n",  # plugin-local relative import is fine
    "class Frame:\n    pass\n\nFrame.size = 3\n",  # local class
    "def register(ctx):\n    ctx.register_tool(name='x')\n    ctx.post_message('hi')\n",
    "class C:\n    def _helper(self):\n        return self._x\n",
    "doc = 'Never read the env file or the auth file.'\n",
    "url = 'https://example.com/api/v1/status'\n",
    "import ast\ntree = ast.literal_eval('[1, 2]')\n",
    "import re\npattern = re.compile(r'\\d+')\n",
    "import sys\nmods = list(sys.modules)\n",
    "import os\nos.makedirs('x', exist_ok=True)\n",
    "import os  # rule-lint: ignore[11]\nv = os.environ['TZ']  # rule-lint: ignore[11] tz only\n",
]


@pytest.mark.parametrize("source", PY_GOOD)
def test_python_good_samples(tmp_path: Path, source: str) -> None:
    assert _lint(tmp_path, "mod.py", source) == []


def test_test_code_may_use_monkeypatch_and_sys_modules(tmp_path: Path) -> None:
    source = (
        "import sys, types\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr('agent.x', 1)\n"
        "    sys.modules['hermes_plugins'] = types.ModuleType('hermes_plugins')\n"
        "    del sys.modules['hermes_plugins']\n"
    )
    assert _lint(tmp_path, "tests/test_x.py", source) == []
    assert _lint(tmp_path / "b", "tests/conftest.py", source) == []


def test_test_code_still_checked_for_secrets(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "tests/test_x.py", "import os\nk = os.environ['X']\n")
    assert _rules(findings) == {11}


def test_suppression_is_rule_specific(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "mod.py", "eval('1')  # rule-lint: ignore[11]\n")
    assert _rules(findings) == {7}


def test_line_numbers_are_reported(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "pkg/mod.py", "x = 1\n\nimport os\ny = os.getenv('A')\n")
    assert [(f.path, f.line, f.rule) for f in findings] == [("pkg/mod.py", 4, 11)]


def test_syntax_error_is_reported(tmp_path: Path) -> None:
    assert _rules(_lint(tmp_path, "mod.py", "def (:\n")) == {7}


# ---- JavaScript (rule 8) --------------------------------------------------------

JS_BAD = [
    "eval('x')",
    "const f = new Function('a', 'return a')",
    "setTimeout('alert(1)', 10)",
    "const m = await import('https://evil.example/x.js')",
    "const m = await import(name)",
    "const m = await import('lodash')",
    "document.querySelector('[data-slot=sidebar]')",
    "document.querySelectorAll('[data-testid=\"chat\"]')",
    "el.querySelector('[data-sidebar]')",
    "document.querySelector('[data-tour=\"x\"]')",
    "document.querySelector('[data-tree-item]')",
    "btn.closest('.panel')",
    "new MutationObserver(cb).observe(document.body, {childList: true})",
    "Storage.prototype.setItem = function () {}",
    "Array.prototype['flat'] = null",
    "Object.defineProperty(HTMLElement.prototype, 'x', {})",
    "obj.__proto__ = other",
    "const s = document.createElement('script')",
    "el.innerHTML = '<script src=x></script>'",
    "window.hermesDesktop.invoke('x')",
]


@pytest.mark.parametrize("source", JS_BAD)
def test_js_bad_samples(tmp_path: Path, source: str) -> None:
    findings = _lint(tmp_path, "desktop/plugin.js", source + "\n")
    assert _rules(findings) == {8}, findings


JS_GOOD = """\
import { definePlugin, usePanel } from '@hermes/plugin-sdk';
import React from 'react';
// eval('x') in a comment is ignored, as is document.body observation talk
/* new Function('x') inside a block comment */
const sdk = await import('@hermes/plugin-sdk');
const sub = await import('@hermes/plugin-sdk/ui');
const R = await import('react');
const label = 'evaluate the frame';
function evaluate(x) { return x; }
class Overlay {}
export default definePlugin({ id: 'hermes-sentinel', panel: () => React.createElement('div') });
"""


def test_js_good_sample(tmp_path: Path) -> None:
    assert _lint(tmp_path, "desktop/plugin.js", JS_GOOD) == []


def test_js_string_contents_survive_comment_stripping(tmp_path: Path) -> None:
    source = "const u = 'http://x//y'; eval(u)\n"
    assert _rules(_lint(tmp_path, "desktop/plugin.js", source)) == {8}


def test_js_suppression(tmp_path: Path) -> None:
    source = "Vec3.prototype.add = add; // rule-lint: ignore[8] bundled lib on its own class\n"
    assert _lint(tmp_path, "desktop/plugin.js", source) == []


# ---- binaries and bytecode (rule 17) --------------------------------------------

BINARY_BAD = [
    ("tool", b"\x7fELF\x02\x01\x01\x00"),
    ("tool.dylib", b"\xcf\xfa\xed\xfe\x07\x00"),
    ("tool.exe", b"MZ\x90\x00\x03\x00"),
    ("mod.wasm", b"\x00asm\x01\x00\x00\x00"),
    ("bundle.zip", b"PK\x03\x04\x14\x00"),
    ("bundle.tgz", b"\x1f\x8b\x08\x00"),
    ("__pycache__/x.cpython-314.pyc", b"\x0d\x0d\x0d\x0a\x00\x00"),
    ("x.pyo", b"not really bytecode"),
]


@pytest.mark.parametrize(("name", "payload"), BINARY_BAD)
def test_binary_bad_samples(tmp_path: Path, name: str, payload: bytes) -> None:
    assert _rules(_lint(tmp_path, name, payload)) == {17}


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("logo.png", b"\x89PNG\r\n\x1a\n"),
        ("photo.jpg", b"\xff\xd8\xff\xe0"),
        ("README.md", b"# Hermes Sentinel\n"),
        ("data.json", b'{"MZ": 1}'),
    ],
)
def test_binary_good_samples(tmp_path: Path, name: str, payload: bytes) -> None:
    assert _lint(tmp_path, name, payload) == []


def test_git_ignored_bytecode_is_skipped_unless_all_files(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "__pycache__").mkdir(parents=True)
    (plugin / "__pycache__" / "x.pyc").write_bytes(b"\x0d\x0d\x0d\x0a")
    (plugin / ".gitignore").write_text("__pycache__/\n")
    (plugin / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=plugin, check=True)
    assert rule_lint.lint_paths(plugin) == []
    assert _rules(rule_lint.lint_paths(plugin, all_files=True)) == {17}


# ---- CLI ------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "mod.py").write_text("x = 1\n")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "mod.py").write_text("import os\nk = os.getenv('K')\n")

    assert rule_lint.main([str(good), "--all-files"]) == 0
    assert rule_lint.main([str(bad), "--all-files"]) == 1
    out = capsys.readouterr().out
    assert "mod.py:2: [rule 11]" in out
    assert rule_lint.main([str(tmp_path / "missing")]) == 2


def test_real_plugin_is_clean() -> None:
    """The shipped plugin must pass its own gate."""
    findings = rule_lint.lint_paths(rule_lint.DEFAULT_TARGET)
    assert findings == [], "\n".join(f.format() for f in findings)


# ---- whole-file JS scanning (rule 8) ----------------------------------------------

JS_MULTILINE_BAD = [
    ("const m = await import(\n  'https://evil.example/x.js'\n)\n", 1),
    ("const m = await import(\n  name\n)\n", 1),
    ("new MutationObserver(cb).observe(\n  document.body,\n  {childList: true}\n)\n", 1),
    ("const s = document.createElement(\n  'script'\n)\n", 1),
    ("x = 1;\nObject.defineProperty(\n  HTMLElement.prototype, 'x', {})\n", 2),
    ("const f = new\nFunction('a', 'return a')\n", 1),
    ("EVAL('x')\n", 1),
    ("el.innerHTML = '<SCRIPT src=x></SCRIPT>'\n", 1),
    ("document.querySelector('[DATA-SLOT=x]')\n", 1),
    ("window\n  .hermesDesktop.invoke('x')\n", 1),
    ("document.write('<b>x</b>')\n", 1),
]


@pytest.mark.parametrize(("source", "line"), JS_MULTILINE_BAD)
def test_js_patterns_span_lines_and_ignore_case(tmp_path: Path, source: str, line: int) -> None:
    findings = _lint(tmp_path, "desktop/plugin.js", source)
    assert _rules(findings) == {8}, findings
    assert findings[0].line == line


def test_js_line_mapping_after_multiline_comment(tmp_path: Path) -> None:
    source = "/* a\n b\n c */\nconst ok = 1;\n\neval('x')\n"
    findings = _lint(tmp_path, "desktop/plugin.js", source)
    assert [(f.line, f.rule) for f in findings] == [(6, 8)]


def test_js_multiline_suppression_applies_to_start_line(tmp_path: Path) -> None:
    source = (
        "const m = await import( // rule-lint: ignore[8] reviewed: vendored worker shim\n"
        "  './worker.js')\n"
    )
    assert _lint(tmp_path, "desktop/plugin.js", source) == []


# ---- suppressions need a justification ------------------------------------------


@pytest.mark.parametrize(
    "comment",
    ["# rule-lint: ignore[7]", "# rule-lint: ignore[7]   ", "# rule-lint: ignore[7] -- ."],
)
def test_bare_suppression_is_ignored(tmp_path: Path, comment: str) -> None:
    findings = _lint(tmp_path, "mod.py", f"eval('1')  {comment}\n")
    assert _rules(findings) == {7}
    assert "add a justification" in findings[0].message


def test_justified_suppression_is_honoured(tmp_path: Path) -> None:
    source = "eval('1')  # rule-lint: ignore[7] constant literal in a doc example\n"
    assert _lint(tmp_path, "mod.py", source) == []


def test_bare_js_suppression_is_ignored(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "desktop/plugin.js", "eval('x') // rule-lint: ignore[8]\n")
    assert _rules(findings) == {8} and "add a justification" in findings[0].message


# ---- more binary formats (rule 17) ----------------------------------------------


def _tar_bytes() -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("payload.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _pyc_bytes() -> bytes:
    import importlib.util

    return importlib.util.MAGIC_NUMBER + b"\x00" * 12 + b"\xe3" + b"\x00" * 32


MORE_BINARY_BAD = [
    ("assets/data.bin", _tar_bytes()),  # tar has no magic at offset 0
    ("assets/notes.txt", _tar_bytes()),  # extension does not matter
    ("model.zst", b"\x28\xb5\x2f\xfd\x00\x00"),
    ("empty.zip", b"PK\x05\x06" + b"\x00" * 18),
    ("spanned.zip", b"PK\x07\x08" + b"\x00" * 8),
    ("lib.a", b"!<arch>\n"),
    ("helper.dat", _pyc_bytes()),  # renamed .pyc
    ("old.bin", (3413).to_bytes(2, "little") + b"\r\n" + b"\x00" * 12),  # py3.8 magic
]


@pytest.mark.parametrize(("name", "payload"), MORE_BINARY_BAD)
def test_more_binary_formats(tmp_path: Path, name: str, payload: bytes) -> None:
    assert _rules(_lint(tmp_path, name, payload)) == {17}


@pytest.mark.parametrize(
    "payload",
    [
        b"ab\r\nplain text with crlf line endings\r\n",
        b"x" * 300,  # long text, nothing at offset 257
        b"\x0b\x0c\r\n" + b"\xff" * 12,  # pyc-like version but invalid flags
    ],
)
def test_binary_heuristics_do_not_flag_text(tmp_path: Path, payload: bytes) -> None:
    assert _lint(tmp_path, "notes.txt", payload) == []


# ---- subprocess list arguments (rule 3) ------------------------------------------

SUBPROCESS_BAD = [
    "import subprocess\nsubprocess.run(['git', 'pull'])\n",
    "import subprocess\nsubprocess.check_call(['git', 'fetch', '--all'], cwd=d)\n",
    "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', '-U', p])\n",
    "from subprocess import Popen\nPopen(('pip', 'install', 'x'))\n",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('git', 'pull')\n",
    "import os\nos.execvp('npm', ['npm', 'install'])\n",
    "import subprocess as sp\nsp.run(['uv', 'pip', 'install', 'x'])\n",
]


@pytest.mark.parametrize("source", SUBPROCESS_BAD)
def test_subprocess_list_args_are_joined(tmp_path: Path, source: str) -> None:
    assert 3 in _rules(_lint(tmp_path, "mod.py", source))


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['git', 'status'])\n",
        "import subprocess\nsubprocess.run(['ffmpeg', '-i', 'x'])\n",
        "def run(*a):\n    pass\nrun('git', 'pull')\n",  # not subprocess
    ],
)
def test_subprocess_harmless_commands(tmp_path: Path, source: str) -> None:
    assert 3 not in _rules(_lint(tmp_path, "mod.py", source))


# ---- aliases tracked through assignment ------------------------------------------

ALIAS_BAD = [
    # __file__-derived paths written in a module that does network I/O (rule 3)
    (
        "import urllib.request\nfrom pathlib import Path\n"
        "here = Path(__file__).parent\ntarget = here / 'plugin.py'\n"
        "target.write_bytes(urllib.request.urlopen(u).read())\n",
        3,
    ),
    (
        "import os, requests\nroot = os.path.dirname(os.path.abspath(__file__))\n"
        "with open(os.path.join(root, 'x.py'), 'w') as fh:\n    fh.write(requests.get(u).text)\n",
        3,
    ),
    # sys.modules aliases (rule 9)
    ("import sys\nmods = sys.modules\nmods['agent'] = object()\n", 9),
    ("import sys\nmods = sys.modules\nmods.pop('agent')\n", 9),
    ("from sys import modules\nmodules['agent'] = 1\n", 9),
    ("import sys as s\ns.modules['agent'] = 1\n", 9),
    ("import sys\nm = sys.modules\ndel m['agent']\n", 9),
    # Hermes module / class aliases (rule 9)
    ("from run_agent import AIAgent\nA = AIAgent\nA.run = None\n", 9),
    ("import hermes_cli.plugins as hp\nPM = hp.PluginManager\nPM.load = None\n", 9),
    ("from gateway import server\nsrv = server\nsetattr(srv, 'handle', None)\n", 9),
    ("import agent.plugin_llm as pl\nllm_mod = pl\nllm_mod.PluginLlm = None\n", 9),
]


@pytest.mark.parametrize(("source", "rule"), ALIAS_BAD)
def test_aliases_are_tracked(tmp_path: Path, source: str, rule: int) -> None:
    assert rule in _rules(_lint(tmp_path, "mod.py", source))


@pytest.mark.parametrize(
    "source",
    [
        # rebinding drops the alias
        "import sys\nmods = sys.modules\nmods = {}\nmods['x'] = 1\n",
        # __file__-derived path without network I/O is fine (reading own assets)
        "from pathlib import Path\nhere = Path(__file__).parent\n"
        "(here / 'cache.json').write_text('{}')\n",
        # network I/O writing to plugin data, not to __file__
        "import urllib.request\nfrom plugins.plugin_storage import plugin_data_dir\n"
        "d = plugin_data_dir('hermes-sentinel')\n"
        "(d / 'snap.jpg').write_bytes(urllib.request.urlopen(u).read())\n",
        "from agent.secret_scope import get_secret\nget = get_secret\nk = get('X')\n",
    ],
)
def test_alias_good_samples(tmp_path: Path, source: str) -> None:
    assert _lint(tmp_path, "mod.py", source) == []


# ---- test-code exemption is limited to the top-level tests dir --------------------

RUNTIME_ONLY = (
    "import sys, types\nsys.modules['hermes_plugins'] = types.ModuleType('hermes_plugins')\n"
)


@pytest.mark.parametrize(
    "name",
    [
        "sentinel/tests/helpers.py",
        "sentinel/test_utils.py",
        "sentinel/conftest.py",
        "pkg/tests/conftest.py",
        "test_root.py",
    ],
)
def test_nested_tests_dirs_are_not_exempt(tmp_path: Path, name: str) -> None:
    assert 9 in _rules(_lint(tmp_path, name, RUNTIME_ONLY))


@pytest.mark.parametrize("name", ["tests/test_x.py", "tests/harness/shims.py", "conftest.py"])
def test_top_level_tests_dir_is_exempt(tmp_path: Path, name: str) -> None:
    assert _lint(tmp_path, name, RUNTIME_ONLY) == []


def test_is_test_path() -> None:
    assert rule_lint.is_test_path("tests/test_x.py")
    assert rule_lint.is_test_path("tests/harness/fake_ctx.py")
    assert rule_lint.is_test_path("conftest.py")
    assert not rule_lint.is_test_path("tests")  # a file literally named tests
    assert not rule_lint.is_test_path("sentinel/tests/x.py")
    assert not rule_lint.is_test_path("test_x.py")
