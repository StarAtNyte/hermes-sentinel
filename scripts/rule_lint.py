#!/usr/bin/env python3
"""Static checker for the Hermes plugin catalog admission rules.

Scans a plugin directory (default: ../sentinel-vision relative to this script) and
reports violations of the catalog rules listed in the project CLAUDE.md:

  rule 3   self-updating code (downloading .py/.js/archives, writing over __file__)
  rule 7   eval/exec/compile of dynamic code (would trip the install security scan)
  rule 8   desktop JS escaping the Desktop Plugin SDK
  rule 9   runtime overrides of Hermes core, sys.modules writes, private internals
  rule 11  credentials read outside secret_scope (os.environ, .env, auth.json)
  rule 17  compiled binaries, bytecode, archives

Output: one ``path:line: [rule N] message`` line per finding; exit 1 on findings,
0 when clean, 2 on usage errors.

Suppress a reviewed false positive on the same line with
``# rule-lint: ignore[9] <why>`` (Python) or ``// rule-lint: ignore[8] <why>`` (JS).
The justification text is mandatory: a bare ``ignore[N]`` suppresses nothing and the finding
is reported with a note.

Test code (only the plugin's top-level ``tests/`` directory and a top-level ``conftest.py``)
may use monkeypatch / mock / ``sys.modules`` for isolation; every other rule still applies.

Standard library only, so it runs in CI before any dependency is installed.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = REPO_ROOT / "sentinel-vision"

# Top-level packages and modules of hermes-agent (checked against v0.22.5).
HERMES_PACKAGES = frozenset(
    {
        "acp_adapter",
        "agent",
        "batch_runner",
        "cli",
        "cron",
        "gateway",
        "hermes_cli",
        "hermes_platform",
        "mini_swe_runner",
        "model_tools",
        "plugins",
        "pm",
        "providers",
        "registration_lifecycle",
        "run_agent",
        "toolset_distributions",
        "toolsets",
        "tools",
        "trajectory_compressor",
        "tui_gateway",
        "utils",
    }
)
HERMES_PREFIXES = ("hermes_",)  # hermes_constants, hermes_state*, hermes_logging, ...

SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", ".ruff_cache", ".pytest_cache"})
PY_SUFFIXES = frozenset({".py", ".pyi"})
JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})

# (magic prefix, description). Images, fonts and other media are allowed.
BINARY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "ELF binary"),
    (b"\xfe\xed\xfa\xce", "Mach-O binary"),
    (b"\xfe\xed\xfa\xcf", "Mach-O binary"),
    (b"\xce\xfa\xed\xfe", "Mach-O binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O binary"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary / Java class"),
    (b"MZ", "PE/DOS executable"),
    (b"\x00asm", "WebAssembly module"),
    (b"PK\x03\x04", "zip archive"),
    (b"PK\x05\x06", "zip archive (empty)"),
    (b"PK\x07\x08", "zip archive (spanned)"),
    (b"PK\x01\x02", "zip archive (central directory)"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (b"Rar!\x1a\x07", "rar archive"),
    (b"\x28\xb5\x2f\xfd", "zstd archive"),
    (b"\x04\x22\x4d\x18", "lz4 archive"),
    (b"MSCF", "cab archive"),
    (b"!<arch>\n", "ar archive / static library"),
    (b"\xed\xab\xee\xdb", "rpm package"),
)
# POSIX/GNU tar: "ustar" magic at offset 257 (no magic at offset 0).
TAR_MAGIC_OFFSET = 257
TAR_MAGIC = b"ustar"
# CPython bytecode: 2-byte little-endian version magic + b"\r\n", then a 4-byte flags field
# (PEP 552: 0, 1 or 3). Python 3.0 .. 3.x magic numbers live in 3000..3999.
PYC_MAGIC_RANGE = range(3000, 4000)
PYC_FLAGS = frozenset({0, 1, 3})
HEAD_BYTES = 512

SUPPRESS_RE = re.compile(r"rule-lint:\s*ignore\[([\d,\s]+)\]([^\n]*)")
# A justification needs at least one real word after the bracket (not just punctuation).
JUSTIFICATION_RE = re.compile(r"[A-Za-z]{2,}")
UNJUSTIFIED_NOTE = " (suppression ignored: add a justification after ignore[...])"

# Credential files a plugin must never read (rule 11).
SECRET_PATH_RE = re.compile(
    r"(^|[/\\~])\.env(\.[\w.-]+)?$|(^|[/\\])auth\.json$|(^|[/\\])\.netrc$|"
    r"(^|[/\\])credentials(\.json)?$|\.git-credentials$",
    re.IGNORECASE,
)
# URLs pointing at code or archives: fetching these is a self-update channel (rule 3).
CODE_URL_RE = re.compile(
    r"^https?://\S+\.(py|pyc|js|mjs|cjs|ts|zip|whl|tar|tgz|tar\.gz|so|dll|dylib|wasm)(\?\S*)?$",
    re.IGNORECASE,
)
SELF_UPDATE_SHELL_RE = re.compile(
    r"\bgit\s+(pull|fetch|reset|clone|checkout)\b|\bpip3?\s+install\b|\bnpm\s+(install|i)\b"
    r"|\buv\s+(pip\s+install|add|sync)\b",
    re.IGNORECASE,
)
# Calls that take a command as a list (or as varargs): their parts are joined before matching.
SUBPROCESS_FUNCS = frozenset(
    {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "getoutput",
        "getstatusoutput",
        "system",
        "popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "spawnv",
        "spawnvp",
        "spawnl",
        "spawnlp",
        "execv",
        "execvp",
        "execl",
        "execlp",
    }
)

NETWORK_CALLS = frozenset(
    {
        "urlopen",
        "urlretrieve",
        "Request",
        "get",
        "post",
        "request",
        "stream",
        "download",
        "download_file",
        "fetch",
    }
)
NETWORK_MODULES = frozenset({"urllib", "requests", "httpx", "aiohttp", "urllib3", "http"})
WRITE_ATTRS = frozenset(
    {"write_text", "write_bytes", "copy", "copyfile", "copy2", "move", "replace", "rename"}
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: int
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: [rule {self.rule}] {self.message}"


def _is_hermes_module(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in HERMES_PACKAGES or top.startswith(HERMES_PREFIXES)


def _is_private(segment: str) -> bool:
    return segment.startswith("_") and not (segment.startswith("__") and segment.endswith("__"))


def _suppression(lines: list[str], lineno: int, rule: int) -> str | None:
    """``"ok"`` when the line suppresses ``rule`` with a justification, ``"unjustified"`` when it
    names the rule without one, ``None`` when it does not mention the rule."""
    if 1 <= lineno <= len(lines):
        match = SUPPRESS_RE.search(lines[lineno - 1])
        if match and str(rule) in {r.strip() for r in match.group(1).split(",")}:
            return "ok" if JUSTIFICATION_RE.search(match.group(2)) else "unjustified"
    return None


def _apply_suppressions(findings: Iterable[Finding], lines: list[str]) -> list[Finding]:
    out: list[Finding] = []
    for f in findings:
        status = _suppression(lines, f.line, f.rule)
        if status == "ok":
            continue
        if status == "unjustified":
            f = Finding(f.path, f.line, f.rule, f.message + UNJUSTIFIED_NOTE)
        out.append(f)
    return out


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _PyVisitor(ast.NodeVisitor):
    """Collects findings for one Python file."""

    def __init__(self, rel: str, is_test: bool = False) -> None:
        self.rel = rel
        # Test code may use pytest's monkeypatch / unittest.mock and register modules in
        # sys.modules (scoped test isolation, not a runtime override). All other rules apply.
        self.is_test = is_test
        self.findings: list[Finding] = []
        # local name -> fully qualified import origin, for absolute imports only
        self.imported: dict[str, str] = {}
        self.uses_network = False
        self.network_line = 0
        self.file_writes: list[int] = []
        # names bound to a path derived from __file__ (``here = Path(__file__).parent``)
        self.file_derived: set[str] = set()

    def add(self, node: ast.AST, rule: int, message: str) -> None:
        self.findings.append(Finding(self.rel, getattr(node, "lineno", 1), rule, message))

    # ---- imports --------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            self.imported[local] = alias.name if alias.asname else local
            self._check_import(node, alias.name)
            if alias.name.split(".", 1)[0] in NETWORK_MODULES:
                self._mark_network(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:  # relative import: plugin-local code
            self.generic_visit(node)
            return
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}"
            self.imported[alias.asname or alias.name] = full
            self._check_import(node, full)
            if module == "os" and alias.name in {"environ", "getenv", "environb", "getenvb"}:
                self.add(
                    node,
                    11,
                    f"'from os import {alias.name}': read secrets via "
                    "agent.secret_scope.get_secret(), not the process environment",
                )
        if module.split(".", 1)[0] in NETWORK_MODULES:
            self._mark_network(node)
        self.generic_visit(node)

    def _check_import(self, node: ast.AST, dotted: str) -> None:
        if _is_hermes_module(dotted) and any(_is_private(p) for p in dotted.split(".")):
            self.add(node, 9, f"import of private Hermes internal '{dotted}'")
        top = dotted.split(".", 1)[0]
        if top == "importlib" and dotted.endswith(".reload"):
            self.add(node, 9, "importlib.reload rebinds module objects")

    def _mark_network(self, node: ast.AST) -> None:
        if not self.uses_network:
            self.uses_network = True
            self.network_line = getattr(node, "lineno", 1)

    def _origin(self, name: str | None) -> str | None:
        return self.imported.get(name) if name else None

    def _resolve(self, dotted: str) -> str:
        """``dotted`` with its root replaced by the import (or alias) origin it names."""
        if not dotted:
            return dotted
        root, _, rest = dotted.partition(".")
        origin = self._origin(root)
        if origin is None:
            return dotted
        return f"{origin}.{rest}" if rest else origin

    def _is_sys_modules(self, node: ast.AST) -> bool:
        return self._resolve(_dotted(node)) == "sys.modules"

    def _track_alias(self, target: ast.AST, value: ast.AST | None) -> None:
        """Follow simple ``name = <imported name or attribute>`` and ``__file__`` aliases."""
        if not isinstance(target, ast.Name) or value is None:
            return
        name = target.id
        base = value
        while isinstance(base, ast.Attribute):
            base = base.value
        dotted = _dotted(value) if isinstance(base, ast.Name) else ""
        root = dotted.split(".", 1)[0] if dotted else None
        if dotted and root in self.imported:
            self.imported[name] = self._resolve(dotted)
        else:
            self.imported.pop(name, None)  # rebinding drops a stale alias
        if self._mentions_file(value):
            self.file_derived.add(name)
        else:
            self.file_derived.discard(name)

    def _is_hermes_name(self, name: str | None) -> bool:
        origin = self._origin(name)
        return bool(origin) and _is_hermes_module(origin)

    # ---- attribute reads ------------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted(node)
        root = dotted.split(".", 1)[0] if dotted else None
        origin = self._origin(root)
        if origin == "os" and node.attr in {"environ", "environb"}:
            self.add(
                node,
                11,
                "os.environ access: read secrets via "
                "agent.secret_scope.get_secret(NAME) declared in requires_env",
            )
        elif origin == "posix" and node.attr == "environ":
            self.add(node, 11, "posix.environ access")
        if (
            _is_private(node.attr)
            and isinstance(node.ctx, ast.Load)
            and (self._is_hermes_name(root) or root == "ctx")
        ):
            self.add(node, 9, f"access to private Hermes attribute '{dotted}'")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self._origin(node.id) in {"os.environ", "os.environb"} and isinstance(
            node.ctx, ast.Load
        ):
            self.add(node, 11, "environ access: read secrets via agent.secret_scope.get_secret()")
        self.generic_visit(node)

    # ---- assignments ----------------------------------------------------------
    def _check_target(self, target: ast.AST, node: ast.AST, verb: str) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._check_target(elt, node, verb)
            return
        if isinstance(target, ast.Subscript) and self._is_sys_modules(target.value):
            # Test loaders register the plugin package in sys.modules the way Hermes does.
            if not self.is_test:
                self.add(node, 9, f"sys.modules {verb}: never replace or inject modules")
            return
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            root = _root_name(target)
            origin = self._origin(root)
            if origin is None:
                return
            if _is_hermes_module(origin):
                self.add(
                    node,
                    9,
                    f"{verb} of attribute on Hermes core object "
                    f"'{_dotted(target) or root}' (runtime override)",
                )
            elif root and root[:1].isupper() or (origin.split(".", 1)[0] in {"builtins", "sys"}):
                self.add(
                    node,
                    9,
                    f"{verb} of attribute on imported '{_dotted(target) or root}' (monkeypatching)",
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_target(target, node, "assignment")
        self.generic_visit(node)
        for target in node.targets:
            self._track_alias(target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_target(node.target, node, "assignment")
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_target(node.target, node, "assignment")
        self.generic_visit(node)
        self._track_alias(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self._track_alias(node.target, node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._check_target(target, node, "deletion")
        self.generic_visit(node)

    # ---- calls ----------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        dotted = _dotted(func)
        root = dotted.split(".", 1)[0] if dotted else None
        origin = self._origin(root)

        if name in {"eval", "exec", "compile"} and name not in self.imported:
            self.add(node, 7, f"{name}() of dynamic code")
        if name == "__import__":
            self.add(node, 9, "__import__() dynamic import")

        if (
            origin == "os" and dotted in {"os.getenv", "os.getenvb", "os.putenv", "os.unsetenv"}
        ) or (name and self._origin(name) in {"os.getenv", "os.getenvb"}):
            self.add(
                node, 11, f"{dotted or name}(): read secrets via agent.secret_scope.get_secret()"
            )

        if name in {"setattr", "delattr"} and node.args:
            self._check_attribute_override(node, name)
        if isinstance(func, ast.Attribute):
            self._check_attribute_call(node, func, origin)
        if name == "open" and self._mentions_file(node) and self._open_for_write(node):
            self.file_writes.append(node.lineno)
        if name and self._origin(name) and self._origin(name).split(".", 1)[0] in NETWORK_MODULES:
            self._mark_network(node)
        self._check_command_args(node, func, origin)
        self.generic_visit(node)

    def _check_attribute_override(self, node: ast.Call, name: str) -> None:
        """Flag ``setattr``/``delattr`` aimed at imported modules, core classes or sys.modules."""
        target_root = _root_name(node.args[0])
        target_origin = self._origin(target_root)
        if target_origin and (
            _is_hermes_module(target_origin)
            or target_root[:1].isupper()
            or target_origin in {"sys", "builtins"}
        ):
            self.add(
                node,
                9,
                f"{name}() on imported '{_dotted(node.args[0]) or target_root}' (runtime override)",
            )
        elif self._is_sys_modules(node.args[0]):
            self.add(node, 9, f"{name}() on sys.modules")

    def _check_attribute_call(
        self, node: ast.Call, func: ast.Attribute, origin: str | None
    ) -> None:
        """Flag module injection, monkeypatching, reloads and network or file-write calls."""
        owner = _dotted(func.value)
        if (
            not self.is_test
            and self._is_sys_modules(func.value)
            and func.attr
            in {"update", "setdefault", "pop", "clear", "__setitem__", "__delitem__", "popitem"}
        ):
            self.add(node, 9, f"sys.modules.{func.attr}(): never replace or inject modules")
        if not self.is_test and func.attr == "setattr" and owner.endswith("monkeypatch"):
            self.add(node, 9, "monkeypatch.setattr in plugin runtime code")
        if (
            not self.is_test
            and func.attr in {"patch", "object"}
            and (owner.split(".")[-1] in {"mock", "patch"})
        ):
            self.add(node, 9, "mock.patch in plugin runtime code")
        if owner in {"importlib"} and func.attr == "reload":
            self.add(node, 9, "importlib.reload rebinds module objects")
        if func.attr in NETWORK_CALLS and origin and origin.split(".", 1)[0] in NETWORK_MODULES:
            self._mark_network(node)
        if func.attr in WRITE_ATTRS and self._mentions_file(node):
            self.file_writes.append(node.lineno)

    def _check_command_args(self, node: ast.Call, func: ast.AST, origin: str | None) -> None:
        """``subprocess.run(["git", "pull"])``: join list/vararg command parts, then match."""
        attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if attr not in SUBPROCESS_FUNCS:
            return
        source = origin if isinstance(func, ast.Attribute) else self._origin(attr)
        if not source or source.split(".", 1)[0] not in {"subprocess", "os", "asyncio"}:
            return
        parts: list[ast.AST] = []
        for arg in node.args:  # flatten: run([...]), exec_("git", "pull"), execvp(f, [...])
            parts.extend(arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg])
        words: list[str] = []
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                if SELF_UPDATE_SHELL_RE.search(part.value):
                    return  # the literal itself is already reported by visit_Constant
                words.append(part.value)
            else:
                resolved = self._resolve(_dotted(part)) if isinstance(part, ast.Attribute) else ""
                words.append("python" if resolved == "sys.executable" else "?")
        command = " ".join(words)
        if SELF_UPDATE_SHELL_RE.search(command):
            self.add(node, 3, f"self-update command {command!r} (list/vararg arguments)")

    def _mentions_file(self, node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Name) and (n.id == "__file__" or n.id in self.file_derived)
            for n in ast.walk(node)
        )

    @staticmethod
    def _open_for_write(node: ast.Call) -> bool:
        mode: ast.AST | None = node.args[1] if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        return (
            isinstance(mode, ast.Constant)
            and isinstance(mode.value, str)
            and any(c in mode.value for c in "wax+")
        )

    # ---- string literals ------------------------------------------------------
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            value = node.value.strip()
            if SECRET_PATH_RE.search(value):
                self.add(
                    node,
                    11,
                    f"reference to credential file {value!r}: never read "
                    ".env/auth.json/token files",
                )
            if CODE_URL_RE.match(value):
                self.add(
                    node,
                    3,
                    f"URL to code/archive {value!r}: plugins must not download "
                    "or replace their own files",
                )
            if SELF_UPDATE_SHELL_RE.search(value):
                self.add(node, 3, f"self-update shell command {value!r}")
        self.generic_visit(node)

    def finish(self) -> None:
        if self.uses_network and self.file_writes:
            for line in self.file_writes:
                self.findings.append(
                    Finding(
                        self.rel,
                        line,
                        3,
                        "writes to a path derived from __file__ in a module that does network I/O "
                        f"(line {self.network_line}): self-update pattern",
                    )
                )


def is_test_path(rel: str) -> bool:
    """True only for the plugin's own pytest code: files under the top-level ``tests/`` dir and a
    top-level ``conftest.py``. A ``tests/`` dir or ``test_*.py`` file deeper in the tree may be
    imported at runtime, so it gets no exemption."""
    parts = rel.split("/")
    return (len(parts) > 1 and parts[0] == "tests") or rel == "conftest.py"


def lint_python(path: Path, rel: str) -> list[Finding]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            Finding(rel, exc.lineno or 1, 7, f"syntax error, file cannot be scanned: {exc.msg}")
        ]
    visitor = _PyVisitor(rel, is_test=is_test_path(rel))
    visitor.visit(tree)
    visitor.finish()
    lines = source.splitlines()
    unique = {(f.line, f.rule, f.message): f for f in visitor.findings}
    return _apply_suppressions(unique.values(), lines)


# ---- JavaScript (rule 8) --------------------------------------------------------

ALLOWED_DYNAMIC_IMPORTS = ("@hermes/plugin-sdk", "react")
# Matched over the whole comment-stripped file (patterns may span lines), case-insensitively.
_JS_FLAGS = re.MULTILINE | re.IGNORECASE
_JS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?<![\w$.])eval\s*\(", "eval()"),
    (r"\bnew\s+Function\s*\(", "new Function()"),
    (r"(?<![\w$.])Function\s*\(\s*['\"`]", "Function() constructor with code string"),
    (r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]", "setTimeout/setInterval with code string"),
    (r"\b[A-Za-z_$][\w$]*\.prototype(?:\.[\w$]+|\[[^\]]+\])\s*=(?!=)", "prototype patching"),
    (
        r"\bObject\.(?:defineProperty|defineProperties|assign|setPrototypeOf)\s*\(\s*"
        r"[\w$.]*\.prototype\b",
        "prototype patching via Object.*",
    ),
    (r"\.__proto__\s*=(?!=)", "__proto__ assignment"),
    (
        r"\bdata-(?:slot|testid|sidebar|tour|tree-[\w-]*)\b",
        "reaching into app markup (data-slot/data-testid/data-sidebar/data-tour/data-tree-*)",
    ),
    (r"\.closest\s*\(", "closest() reaches into app markup"),
    (r"\.observe\s*\(\s*document\.(?:body|documentElement)\b", "MutationObserver on document.body"),
    (r"createElement\s*\(\s*['\"`]script['\"`]", "script-tag injection"),
    (r"<script\b", "script-tag injection"),
    (r"\bwindow\s*\.\s*hermesDesktop\b", "raw window.hermesDesktop bridge call; use the SDK"),
    (r"\bimportScripts\s*\(", "importScripts() loads remote code"),
    (r"\bdocument\.write(?:ln)?\s*\(", "document.write() can inject script tags"),
)
JS_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, _JS_FLAGS), message) for pattern, message in _JS_PATTERNS
)
DYNAMIC_IMPORT_RE = re.compile(r"(?<![\w$.])import\s*\(\s*(?:(['\"`])([^'\"`]*)\1)?", re.MULTILINE)


def _strip_js_comments(source: str) -> str:
    """Blank out comments while keeping line numbers and string contents intact."""
    out: list[str] = []
    i, n = 0, len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            comment = source[i:end]
            # keep suppression markers visible
            out.append(comment if SUPPRESS_RE.search(comment) else " " * len(comment))
            i = end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join(c if c == "\n" else " " for c in source[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _line_starts(text: str) -> list[int]:
    """Offsets where each line begins (index i -> line i + 1)."""
    starts = [0]
    starts.extend(m.end() for m in re.finditer(r"\n", text))
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset)


def lint_js(path: Path, rel: str) -> list[Finding]:
    """Rule 8 over the whole file: patterns may span lines (``import(\n 'x')``), matching is
    case-insensitive, and each finding is reported on the line where its match starts."""
    source = path.read_text(encoding="utf-8", errors="replace")
    stripped = _strip_js_comments(source)
    starts = _line_starts(stripped)
    findings: dict[tuple[int, str], Finding] = {}

    def add(offset: int, message: str) -> None:
        line = _line_of(starts, offset)
        findings.setdefault((line, message), Finding(rel, line, 8, message))

    for pattern, message in JS_RULES:
        for match in pattern.finditer(stripped):
            add(match.start(), message)
    for match in DYNAMIC_IMPORT_RE.finditer(stripped):
        spec = match.group(2)
        if spec is None:
            add(match.start(), "dynamic import() of a non-literal specifier")
        elif not any(spec == a or spec.startswith(a + "/") for a in ALLOWED_DYNAMIC_IMPORTS):
            add(
                match.start(),
                f"dynamic import({spec!r}); only @hermes/plugin-sdk and react are allowed",
            )
    return _apply_suppressions(findings.values(), stripped.splitlines())


# ---- binaries (rule 17) ---------------------------------------------------------


def _looks_like_pyc(head: bytes) -> bool:
    """CPython bytecode header, whatever the file is called (renamed ``.pyc`` payloads)."""
    if len(head) < 16 or head[2:4] != b"\r\n":
        return False
    if head[:4] == importlib.util.MAGIC_NUMBER:
        return True
    version = int.from_bytes(head[0:2], "little")
    flags = int.from_bytes(head[4:8], "little")
    return version in PYC_MAGIC_RANGE and flags in PYC_FLAGS


def lint_binary(path: Path, rel: str) -> list[Finding]:
    if path.suffix.lower() in BYTECODE_SUFFIXES:
        return [Finding(rel, 1, 17, f"compiled bytecode '{path.suffix}' must not be committed")]
    try:
        with path.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return []
    for magic, label in BINARY_MAGIC:
        if head.startswith(magic):
            return [Finding(rel, 1, 17, f"{label} (magic {magic!r}) is an unscannable payload")]
    if head[TAR_MAGIC_OFFSET : TAR_MAGIC_OFFSET + len(TAR_MAGIC)] == TAR_MAGIC:
        return [Finding(rel, 1, 17, "tar archive ('ustar' at 257) is an unscannable payload")]
    if _looks_like_pyc(head):
        return [Finding(rel, 1, 17, "CPython bytecode (renamed .pyc) must not be committed")]
    return []


# ---- file discovery -------------------------------------------------------------


def _git_files(target: Path) -> list[Path] | None:
    """Files git would commit under *target*: tracked plus untracked-not-ignored."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
            cwd=target,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [target / p for p in out.decode("utf-8", "replace").split("\0") if p]


def _walk(target: Path) -> Iterator[Path]:
    for path in sorted(target.rglob("*")):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(target).parts):
            yield path


def iter_files(target: Path, all_files: bool = False) -> list[Path]:
    if target.is_file():
        return [target]
    files = None if all_files else _git_files(target)
    if files is None:
        files = list(_walk(target))
    return sorted(p for p in files if p.is_file())


def lint_paths(target: Path, all_files: bool = False) -> list[Finding]:
    target = target.resolve()
    base = target.parent if target.is_file() else target
    findings: list[Finding] = []
    for path in iter_files(target, all_files):
        rel = path.relative_to(base).as_posix()
        suffix = path.suffix.lower()
        findings.extend(lint_binary(path, rel))
        if suffix in PY_SUFFIXES:
            findings.extend(lint_python(path, rel))
        elif suffix in JS_SUFFIXES:
            findings.extend(lint_js(path, rel))
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule, f.message))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "target",
        nargs="?",
        default=str(DEFAULT_TARGET),
        help=f"plugin directory or file (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="scan every file, including git-ignored ones (default: only files git would commit)",
    )
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    target = Path(args.target)
    if not target.exists():
        print(f"rule_lint: target not found: {target}", file=sys.stderr)
        return 2
    findings = lint_paths(target, args.all_files)
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for finding in findings:
            print(finding.format())
        status = f"{len(findings)} finding(s)" if findings else "clean"
        print(f"rule_lint: {target}: {status}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
