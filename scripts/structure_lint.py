#!/usr/bin/env python3
"""Static structure checker for the plugin tree (the "Enforced by tooling" list in CLAUDE.md).

Scans a directory (default: ../sentinel-vision relative to this script), skipping ``tests/`` and
``__pycache__``, and reports:

  module-length      module longer than --max-module-lines (default 400)
  class-length       class longer than --max-class-lines (default 250)
  function-length    function longer than --max-function-lines (default 40, warning);
                     longer than --max-function-hard (default 60) is an error
  nesting-depth      control-flow nesting deeper than --max-depth (default 3)
  init-logic         ``__init__.py`` holding anything but docstring, imports, ``__all__`` and
                     constants (the plugin root may also hold ``register`` and helpers it calls)
  flat-package       more than --max-package-modules (default 8) modules in one package dir
  missing-docstring  public module, class, function or method without a docstring
  bare-except        ``except:`` with no exception type
  except-pass        ``except Exception: pass`` (a swallowed broad error)
  commented-code     a comment that parses as code (``# x = foo(1)``)
  todo-issue         TODO/FIXME without the word "issue", ``#123`` or an ISO date

Output is one ``path:line: [rule] message`` line per finding. Exit code: 1 when any error is
found, 0 otherwise, 2 on usage errors. ``--report`` prints counts and the top offenders per
rule and always exits 0. ``--json`` prints findings as JSON.

Suppress a reviewed finding on its own line with ``# structure-lint: ignore[rule] reason``.
The reason is mandatory: without one the finding stays and is annotated.

Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

SKIPPED_DIRS = frozenset({"tests", "__pycache__"})
CONTROL_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
TRY_STAR = getattr(ast, "TryStar", ast.Try)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TODO_MARK = re.compile(r"\b(TODO|FIXME)\b")
TODO_TRACKED = re.compile(r"issue|#\d+", re.IGNORECASE)
IGNORE_COMMENT = re.compile(r"structure-lint:\s*ignore\[([^\]]*)\]\s*(.*)")
COMMENT_NOISE = ("noqa", "type:", "pragma", "structure-lint", "rule-lint", "fmt:", "isort:")
BLOCK_STARTERS = ("if", "elif", "else", "for", "while", "def", "class", "try", "except", "with")


@dataclass(frozen=True)
class Limits:
    """Thresholds for the size and shape rules."""

    module_lines: int = 400
    class_lines: int = 250
    function_lines: int = 40
    function_hard: int = 60
    depth: int = 3
    package_modules: int = 8


@dataclass(frozen=True)
class Finding:
    """One rule violation. ``value`` is the measured size, used to rank offenders."""

    path: str
    line: int
    rule: str
    message: str
    severity: str = "error"
    value: int = 0


# ---------------------------------------------------------------------------------------------
# Suppression comments and comment scanning
# ---------------------------------------------------------------------------------------------


def read_comments(source: str) -> dict[int, str]:
    """Return ``{line: comment text without the leading '#'}`` for every comment token."""
    comments: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string.lstrip("#").strip()
    except (tokenize.TokenError, IndentationError):
        pass  # the ast parse reports a syntax error separately; comments are best-effort
    return comments


def parse_ignores(comments: dict[int, str]) -> dict[int, tuple[set[str], str]]:
    """Map line -> (rules, reason) for ``structure-lint: ignore[...]`` comments."""
    ignores: dict[int, tuple[set[str], str]] = {}
    for line, text in comments.items():
        match = IGNORE_COMMENT.search(text)
        if match:
            rules = {part.strip() for part in match.group(1).split(",") if part.strip()}
            ignores[line] = (rules, match.group(2).strip())
    return ignores


def looks_like_code(text: str) -> bool:
    """Heuristic: does this comment body parse as a real statement rather than prose?"""
    text = text.strip()
    if not text or any(noise in text for noise in COMMENT_NOISE):
        return False
    candidates = [text]
    if text.endswith(":") and text.split()[0].rstrip(":(") in BLOCK_STARTERS:
        candidates = [text + " pass"]
    for candidate in candidates:
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        return _is_statement_code(tree)
    return False


def _is_statement_code(tree: ast.Module) -> bool:
    """True when a parsed comment is an assignment, call, import, return, or block header."""
    if len(tree.body) != 1:
        return False
    node = tree.body[0]
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Call)
    if isinstance(node, ast.AnnAssign):
        return node.value is not None
    return isinstance(
        node,
        (
            ast.Assign, ast.AugAssign, ast.Import, ast.ImportFrom, ast.Return, ast.Raise,
            ast.Assert, ast.Delete, ast.If, ast.For, ast.While, ast.Try, ast.With,
            ast.FunctionDef, ast.ClassDef,
        ),
    )  # fmt: skip


# ---------------------------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------------------------


def node_length(node: ast.AST) -> int:
    """Line span of a node, decorators excluded."""
    return (node.end_lineno or node.lineno) - node.lineno + 1  # type: ignore[attr-defined]


def max_nesting(body: list[ast.stmt], depth: int = 0) -> int:
    """Deepest nesting of control-flow statements in ``body``, not entering nested defs."""
    deepest = depth
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, (*CONTROL_NODES, TRY_STAR)):
            child_depth = depth + 1
            deepest = max(deepest, child_depth)
            for block in _blocks(stmt):
                deepest = max(deepest, max_nesting(block, child_depth))
        elif isinstance(stmt, ast.Match):
            deepest = max(deepest, depth + 1)
            for case in stmt.cases:
                deepest = max(deepest, max_nesting(case.body, depth + 1))
    return deepest


def _blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Statement lists directly owned by a compound statement."""
    blocks = [
        getattr(stmt, name) for name in ("body", "orelse", "finalbody") if hasattr(stmt, name)
    ]
    for handler in getattr(stmt, "handlers", []):
        blocks.append(handler.body)
    return blocks


def has_docstring(node: ast.AST) -> bool:
    """True when the module, class or function has a non-empty docstring."""
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    return bool(doc and doc.strip())


def is_overload(node: ast.AST) -> bool:
    """True for ``@overload`` stubs, which need no docstring."""
    for decorator in getattr(node, "decorator_list", []):
        name = (
            decorator.attr if isinstance(decorator, ast.Attribute) else getattr(decorator, "id", "")
        )
        if name == "overload":
            return True
    return False


# ---------------------------------------------------------------------------------------------
# Per-file checks
# ---------------------------------------------------------------------------------------------


class FileChecker:
    """Collects findings for one parsed source file."""

    def __init__(self, path: Path, display: str, source: str, limits: Limits, is_root: bool):
        """Parse ``source`` and prepare to check it; ``is_root`` marks the plugin root package."""
        self.path = path
        self.display = display
        self.source = source
        self.limits = limits
        self.is_root = is_root
        self.findings: list[Finding] = []
        self.comments = read_comments(source)
        self.tree: ast.Module | None = None
        try:
            self.tree = ast.parse(source)
        except SyntaxError as error:
            self._add(error.lineno or 1, "syntax-error", f"cannot parse: {error.msg}")

    def _add(self, line: int, rule: str, message: str, severity: str = "error", value: int = 0):
        """Record a finding."""
        self.findings.append(Finding(self.display, line, rule, message, severity, value))

    def run(self) -> list[Finding]:
        """Run every per-file rule and return the unsuppressed findings."""
        lines = len(self.source.splitlines())
        if lines > self.limits.module_lines:
            self._add(
                1,
                "module-length",
                f"module is {lines} lines (max {self.limits.module_lines})",
                value=lines,
            )
        self._check_comments()
        if self.tree is not None:
            self._check_module_docstring()
            if self.path.name == "__init__.py":
                self._check_init_logic()
            self._visit_scope(self.tree.body, owner_public=True, in_class=False)
            self._check_excepts()
        return self._apply_ignores()

    # -- comments ---------------------------------------------------------------------------

    def _check_comments(self) -> None:
        """Flag commented-out code and untracked TODO/FIXME markers."""
        for line, text in self.comments.items():
            if TODO_MARK.search(text) and not (TODO_TRACKED.search(text) or ISO_DATE.search(text)):
                self._add(line, "todo-issue", "TODO/FIXME needs an issue reference or a date")
            elif looks_like_code(text):
                self._add(line, "commented-code", f"commented-out code: # {text[:60]}")

    # -- docstrings -------------------------------------------------------------------------

    def _check_module_docstring(self) -> None:
        """Public modules (and package ``__init__``) need a docstring."""
        assert self.tree is not None
        name = self.path.name
        public = name == "__init__.py" or not name.startswith("_")
        needs_doc = bool(self.tree.body) or name != "__init__.py"
        if public and needs_doc and not has_docstring(self.tree):
            self._add(1, "missing-docstring", "module has no docstring")

    def _visit_scope(self, body: list[ast.stmt], owner_public: bool, in_class: bool) -> None:
        """Check classes and functions defined directly in ``body``."""
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._check_class(node, owner_public)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._check_function(node, owner_public, in_class)

    def _check_class(self, node: ast.ClassDef, owner_public: bool) -> None:
        """Class length, docstring, and its methods."""
        public = owner_public and not node.name.startswith("_")
        length = node_length(node)
        if length > self.limits.class_lines:
            self._add(
                node.lineno,
                "class-length",
                f"class {node.name} is {length} lines (max {self.limits.class_lines})",
                value=length,
            )
        if public and not has_docstring(node):
            self._add(
                node.lineno, "missing-docstring", f"public class {node.name} has no docstring"
            )
        self._visit_scope(node.body, owner_public=public, in_class=True)

    def _check_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, owner_public: bool, in_class: bool
    ) -> None:
        """Function length, nesting depth and docstring."""
        name = node.name
        dunder = name.startswith("__") and name.endswith("__") and name != "__init__"
        public = (
            owner_public
            and not name.startswith("_")
            or (in_class and name == "__init__" and owner_public)
        )
        length = node_length(node)
        limits = self.limits
        if length > limits.function_hard:
            self._add(
                node.lineno,
                "function-length",
                f"{name}() is {length} lines (hard max {limits.function_hard})",
                value=length,
            )
        elif length > limits.function_lines:
            self._add(
                node.lineno,
                "function-length",
                f"{name}() is {length} lines (max {limits.function_lines})",
                "warning",
                length,
            )
        depth = max_nesting(node.body)
        if depth > limits.depth:
            self._add(
                node.lineno,
                "nesting-depth",
                f"{name}() nests {depth} levels deep (max {limits.depth})",
                value=depth,
            )
        if public and not dunder and not is_overload(node) and not has_docstring(node):
            self._add(
                node.lineno,
                "missing-docstring",
                f"public {'method' if in_class else 'function'} {name}() has no docstring",
            )

    # -- __init__ logic ---------------------------------------------------------------------

    def _check_init_logic(self) -> None:
        """An ``__init__.py`` may re-export; the plugin root may also define ``register``."""
        assert self.tree is not None
        allowed_functions = self._root_function_names() if self.is_root else set()
        for index, node in enumerate(self.tree.body):
            if self._init_node_allowed(node, index, allowed_functions):
                continue
            self._add(node.lineno, "init-logic", f"__init__.py holds logic: {type(node).__name__}")

    def _root_function_names(self) -> set[str]:
        """``register`` plus every module-level function it (transitively) calls."""
        assert self.tree is not None
        defs = {
            n.name: n
            for n in self.tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        allowed: set[str] = set()
        pending = ["register"] if "register" in defs else []
        while pending:
            name = pending.pop()
            if name in allowed or name not in defs:
                continue
            allowed.add(name)
            for call in ast.walk(defs[name]):
                if isinstance(call, ast.Name) and call.id in defs:
                    pending.append(call.id)
        return allowed

    @staticmethod
    def _init_node_allowed(node: ast.stmt, index: int, allowed_functions: set[str]) -> bool:
        """True for docstring, imports, ``__all__``, constants, and permitted root functions."""
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return index == 0 and isinstance(node.value.value, str)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            is_const = len(names) == len(targets) and all(
                n.isupper() or (n.startswith("__") and n.endswith("__")) for n in names
            )
            value = node.value
            simple = (
                isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict))
                or value is None
            )
            return is_const and simple
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name in allowed_functions
        return False

    # -- exceptions -------------------------------------------------------------------------

    def _check_excepts(self) -> None:
        """Flag bare ``except:`` and swallowed broad exceptions."""
        assert self.tree is not None
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                self._add(node.lineno, "bare-except", "bare `except:`; catch a specific exception")
            elif self._is_broad(node.type) and self._body_is_pass(node.body):
                self._add(
                    node.lineno,
                    "except-pass",
                    "`except Exception: pass` swallows errors; log or narrow it",
                )

    @staticmethod
    def _is_broad(expr: ast.expr) -> bool:
        """True for ``Exception`` / ``BaseException`` (alone or in a tuple)."""
        names = expr.elts if isinstance(expr, ast.Tuple) else [expr]
        return any(
            isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names
        )

    @staticmethod
    def _body_is_pass(body: list[ast.stmt]) -> bool:
        """True when a handler body does nothing (pass, ``...`` or a bare docstring)."""
        return all(
            isinstance(s, ast.Pass)
            or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            for s in body
        )

    # -- suppression ------------------------------------------------------------------------

    def _apply_ignores(self) -> list[Finding]:
        """Drop findings with a reasoned ignore on their line; annotate unreasoned ones."""
        ignores = parse_ignores(self.comments)
        kept: list[Finding] = []
        for finding in self.findings:
            rules, reason = ignores.get(finding.line, (set(), ""))
            if finding.rule in rules:
                if reason:
                    continue
                finding = Finding(
                    finding.path, finding.line, finding.rule,
                    finding.message + " (ignore comment ignored: a reason is required)",
                    finding.severity, finding.value,
                )  # fmt: skip
            kept.append(finding)
        return kept


# ---------------------------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------------------------


def iter_python_files(root: Path) -> list[Path]:
    """All ``.py`` files under ``root`` outside skipped and hidden directories."""
    files = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts[:-1]
        if any(part in SKIPPED_DIRS or part.startswith(".") for part in parts):
            continue
        files.append(path)
    return files


def check_packages(root: Path, files: list[Path], limits: Limits) -> list[Finding]:
    """Flag directories with more than ``limits.package_modules`` modules directly inside."""
    per_dir: dict[Path, list[Path]] = defaultdict(list)
    for path in files:
        if path.name != "__init__.py":
            per_dir[path.parent].append(path)
    findings = []
    for directory, modules in sorted(per_dir.items()):
        if len(modules) > limits.package_modules:
            display = str((directory / "__init__.py").relative_to(root.parent))
            findings.append(Finding(
                display, 1, "flat-package",
                f"{len(modules)} modules directly in {directory.name}/ "
                f"(max {limits.package_modules}); group them into sub-packages",
                value=len(modules),
            ))  # fmt: skip
    return findings


def lint_tree(root: Path, limits: Limits) -> list[Finding]:
    """Lint every Python file under ``root`` and return all findings."""
    files = iter_python_files(root)
    findings: list[Finding] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        display = str(path.relative_to(root.parent))
        is_root = path.parent == root
        findings.extend(FileChecker(path, display, source, limits, is_root).run())
    findings.extend(check_packages(root, files, limits))
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule))


# ---------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------


def format_report(findings: list[Finding], top: int = 15) -> str:
    """Counts per rule plus the top offenders (largest measured value first)."""
    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_rule[finding.rule].append(finding)
    counts = Counter({rule: len(items) for rule, items in by_rule.items()})
    out = [f"structure-lint report: {len(findings)} finding(s)", ""]
    out.append("Counts per rule:")
    for rule, count in counts.most_common():
        out.append(f"  {rule:<18} {count}")
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        out.append("")
        out.append(f"Top offenders: {rule} ({len(items)})")
        ranked = sorted(items, key=lambda f: (-f.value, f.path, f.line))[:top]
        out.extend(f"  {f.path}:{f.line}  {f.message}" for f in ranked)
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    default_target = Path(__file__).resolve().parent.parent / "sentinel-vision"
    defaults = Limits()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "target", nargs="?", type=Path, default=default_target, help="plugin directory"
    )
    parser.add_argument(
        "--report", action="store_true", help="print counts and top offenders; exit 0"
    )
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument("--top", type=int, default=15, help="offenders per rule in --report")
    parser.add_argument("--max-module-lines", type=int, default=defaults.module_lines)
    parser.add_argument("--max-class-lines", type=int, default=defaults.class_lines)
    parser.add_argument("--max-function-lines", type=int, default=defaults.function_lines)
    parser.add_argument("--max-function-hard", type=int, default=defaults.function_hard)
    parser.add_argument("--max-depth", type=int, default=defaults.depth)
    parser.add_argument("--max-package-modules", type=int, default=defaults.package_modules)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if not args.target.is_dir():
        print(f"structure-lint: not a directory: {args.target}", file=sys.stderr)
        return 2
    limits = Limits(
        args.max_module_lines, args.max_class_lines, args.max_function_lines,
        args.max_function_hard, args.max_depth, args.max_package_modules,
    )  # fmt: skip
    findings = lint_tree(args.target.resolve(), limits)
    errors = [f for f in findings if f.severity == "error"]
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    elif args.report:
        print(format_report(findings, args.top))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: [{f.rule}] {f.severity}: {f.message}")
        print(f"structure-lint: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 0 if args.report or not errors else 1


if __name__ == "__main__":
    sys.exit(main())
