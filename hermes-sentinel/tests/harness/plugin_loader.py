"""Import the plugin package under the module name Hermes' loader uses.

Hermes imports a directory plugin as ``hermes_plugins.<slug>`` (``slug`` = manifest key with
``-`` -> ``_``; see ``hermes_cli/plugins_loader.py::_directory_module_name``). Tests and the
harness must reach plugin code through that same package, never as a top-level ``sentinel``
module: two import paths would create two copies of every module, and an exception class
raised by one copy would not be caught by an ``except`` naming the other.

:func:`plugin_package` loads the package once per process (thread-safe) and returns the cached
module afterwards; :func:`plugin_module` returns a submodule such as ``"sentinel.sources"``.
:func:`import_plugin_fresh` re-executes the package the way a Hermes force-reload does. Classes
from a fresh import are new objects, so only use it in tests that need module-level state reset
and that do not mix those classes with ones imported earlier.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import types
from pathlib import Path

from . import hermes_shims

__all__ = [
    "MODULE_NAME",
    "NS_PARENT",
    "PLUGIN_DIR",
    "PLUGIN_NAME",
    "evict",
    "import_plugin_fresh",
    "plugin_module",
    "plugin_package",
]

PLUGIN_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = PLUGIN_DIR.name  # == plugin.yaml name (checked in test_register)
NS_PARENT = "hermes_plugins"
MODULE_NAME = f"{NS_PARENT}.{PLUGIN_NAME.replace('/', '__').replace('-', '_')}"

_LOCK = threading.RLock()


def evict(module_name: str = MODULE_NAME) -> None:
    """Drop ``module_name`` and its submodules from ``sys.modules`` (as Hermes does on reload)."""
    with _LOCK:
        for key in [k for k in sys.modules if k == module_name or k.startswith(module_name + ".")]:
            del sys.modules[key]


def _ensure_namespace_parent() -> None:
    if NS_PARENT not in sys.modules:
        ns_pkg = types.ModuleType(NS_PARENT)
        ns_pkg.__path__ = []
        ns_pkg.__package__ = NS_PARENT
        sys.modules[NS_PARENT] = ns_pkg


def _exec_package(module_name: str) -> types.ModuleType:
    init_file = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create a module spec for {init_file}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        evict(module_name)
        raise
    return module


def plugin_package() -> types.ModuleType:
    """The plugin package as ``hermes_plugins.<slug>``, loaded once and then reused."""
    with _LOCK:
        hermes_shims.install()  # module-level Hermes imports in the plugin resolve to shims
        existing = sys.modules.get(MODULE_NAME)
        if existing is not None:
            return existing
        _ensure_namespace_parent()
        return _exec_package(MODULE_NAME)


def plugin_module(relative: str) -> types.ModuleType:
    """A plugin submodule by dotted path relative to the package, e.g. ``"sentinel.gate"``."""
    if not relative or relative.startswith("."):
        raise ValueError(f"expected a relative dotted module path, got {relative!r}")
    with _LOCK:
        plugin_package()
        return importlib.import_module(f"{MODULE_NAME}.{relative}")


def import_plugin_fresh() -> types.ModuleType:
    """Evict and re-import the package (new module and class objects; see module docstring)."""
    with _LOCK:
        hermes_shims.install()
        _ensure_namespace_parent()
        evict(MODULE_NAME)
        return _exec_package(MODULE_NAME)
