"""Optional-dependency hygiene of the sources package."""

from __future__ import annotations

import ast
from typing import Any

import pytest
from harness.plugin_loader import PLUGIN_DIR, plugin_module

src = plugin_module("sentinel.sources")
deps = plugin_module("sentinel.sources.dependencies")
timeouts = plugin_module("sentinel.sources.timeouts")
frames = plugin_module("sentinel.sources.frames")
screen = plugin_module("sentinel.sources.backends.screen")
discovery = plugin_module("sentinel.sources.discovery")
SourceSpec = src.SourceSpec
SourceSpecError = src.SourceSpecError
SourceError = src.SourceError
SourceDisconnected = src.SourceDisconnected
SourceNotReady = src.SourceNotReady
MissingDependency = src.MissingDependency


SOURCES_DIR = PLUGIN_DIR / "sentinel" / "sources"


def _top_level_nodes() -> list[ast.stmt]:
    nodes: list[ast.stmt] = []
    for path in SOURCES_DIR.rglob("*.py"):
        nodes.extend(ast.parse(path.read_text()).body)
    return nodes


def test_optional_deps_not_imported_at_module_level() -> None:
    for node in _top_level_nodes():
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not names & {"cv2", "mss", "numpy", "agent"}, names


def test_no_environ_reads_in_sources() -> None:
    names: set[str] = set()
    for path in SOURCES_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"environ", "getenv", "environb"}


def test_missing_dependency_has_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    real_import = deps.importlib.import_module

    def fake_import(name: str, *a: Any, **k: Any):
        if name in ("cv2", "mss"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(deps.importlib, "import_module", fake_import)
    with pytest.raises(MissingDependency) as exc:
        src.open_source("camera:0").open()
    assert "opencv-python-headless" in str(exc.value)
    assert isinstance(exc.value, SourceError) and isinstance(exc.value, ImportError)
    with pytest.raises(MissingDependency, match="'mss'"):
        src.open_source("screen").open()
    with pytest.raises(MissingDependency):
        src.open_source(f"folder:{tmp_path}").open()
    status = src.dependency_status()
    assert status["cv2"] and "opencv-python-headless" in status["cv2"]
    assert status["numpy"] is None
    # The probe reports the hint instead of raising.
    infos = src.list_sources(total_timeout=2)
    assert any("opencv-python-headless" in i.detail for i in infos if i.kind == "camera")
    assert any("mss" in i.detail for i in infos if i.kind == "screen")
