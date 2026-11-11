"""Runs the node-based checks for sentinel-vision/desktop/plugin.js (skipped without node)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests_tools" / "plugin_js_check.mjs"
PLUGIN_JS = ROOT / "sentinel-vision" / "desktop" / "plugin.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_plugin_js_with_stub_sdk() -> None:
    proc = subprocess.run(
        ["node", str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_plugin_js_passes_rule_lint() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("rule_lint", ROOT / "scripts" / "rule_lint.py")
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    findings = mod.lint_js(PLUGIN_JS, "desktop/plugin.js")
    assert findings == [], [f.format() for f in findings]
