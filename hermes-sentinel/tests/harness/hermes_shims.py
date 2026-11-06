"""Hermetic stand-ins for the Hermes modules a plugin imports at module level.

The plugin may only use these public module APIs (catalog rules 9 and 11):

* ``plugins.plugin_storage.plugin_data_dir(name)`` / ``plugin_db(name, filename)``
* ``agent.secret_scope.get_secret(name, default)`` / ``get_secret_str(name, default)``
* ``agent.plugin_llm`` data classes and ``PluginLlmTrustError`` (``ctx.llm`` itself is the
  :class:`~harness.fake_ctx.FakeLlm`)

:func:`install` puts one module object per name into ``sys.modules`` for the whole test session.
The objects never change, so a plugin that did ``from agent.secret_scope import get_secret``
at import time keeps a working reference across tests. What changes per test is the *binding*
(Hermes home, secrets) held in :data:`STATE`; :func:`bind` sets it and the returned handle's
``close()`` resets it. The real Hermes modules are never imported, so tests never touch
``~/.hermes``. :func:`uninstall` restores whatever was in ``sys.modules`` before.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
import types
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import llm_types

__all__ = [
    "STATE",
    "SHIMMED_MODULES",
    "ShimBinding",
    "ShimState",
    "UnboundShimError",
    "bind",
    "install",
    "installed",
    "uninstall",
]

SHIMMED_MODULES = ("agent", "agent.plugin_llm", "agent.secret_scope", "plugins",
                   "plugins.plugin_storage")

# Same shape `plugins/plugin_storage.py` accepts (no separators or traversal).
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_SECRET_READ_LOG_MAX = 10_000


class UnboundShimError(RuntimeError):
    """A shim was used outside a test binding (no fixture bound a Hermes home / secrets)."""


@dataclass
class ShimState:
    """Per-test binding behind the session-wide shim modules. Thread-safe."""

    home: Path | None = None
    secrets: dict[str, str] = field(default_factory=dict)
    secrets_read: deque[str] = field(default_factory=lambda: deque(maxlen=_SECRET_READ_LOG_MAX))
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def reset(self) -> None:
        with self.lock:
            self.home = None
            self.secrets = {}
            self.secrets_read.clear()


STATE = ShimState()


# ----------------------------------------------------------------------------- storage


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"invalid plugin name for storage: {name!r}")
    return name


def plugin_data_dir(name: str) -> Path:
    """``<bound hermes home>/plugin-data/<name>/`` (created), like the real function."""
    with STATE.lock:
        home = STATE.home
    if home is None:
        raise UnboundShimError(
            "plugins.plugin_storage used with no Hermes home bound; use the plugin_data_root "
            "or fake_ctx fixture"
        )
    root = home / "plugin-data" / _validate_name(name)
    root.mkdir(parents=True, exist_ok=True)
    return root


def plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection:
    """Open ``<data dir>/<filename>`` with the real PRAGMA set: WAL, foreign keys, busy timeout,
    ``check_same_thread=False``, no row factory. The caller owns transactions and closing."""
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError(f"invalid plugin db filename: {filename!r}")
    path = plugin_data_dir(name) / filename
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    try:
        conn.row_factory = None
        conn.execute("PRAGMA busy_timeout=5000")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            raise sqlite3.OperationalError(f"could not enable WAL on {path} (got {mode!r})")
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


# ----------------------------------------------------------------------------- secrets


def get_secret(name: str, default: str | None = None) -> str | None:
    """Look ``name`` up in the bound secrets dict (never ``os.environ``); records the read."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"secret name must be a non-empty string, got {name!r}")
    with STATE.lock:
        STATE.secrets_read.append(name)
        return STATE.secrets.get(name, default)


def get_secret_str(name: str, default: str = "") -> str:
    """``get_secret`` that returns ``default`` (a ``str``) when the secret is unset."""
    value = get_secret(name, default)
    return default if value is None else value


# ----------------------------------------------------------------------------- binding


class ShimBinding:
    """Handle returned by :func:`bind`; ``close()`` clears the binding (idempotent)."""

    def __init__(self) -> None:
        self._closed = False

    @property
    def secrets(self) -> dict[str, str]:
        """The live secrets dict (mutate it to add or remove secrets mid-test)."""
        return STATE.secrets

    @property
    def secrets_read(self) -> list[str]:
        """Names passed to ``get_secret`` since the binding started, in call order."""
        with STATE.lock:
            return list(STATE.secrets_read)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            STATE.reset()

    def __enter__(self) -> ShimBinding:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def bind(*, home: Path | str | None, secrets: Mapping[str, str] | None = None) -> ShimBinding:
    """Bind the shims to a Hermes home and a secrets dict for one test."""
    install()
    with STATE.lock:
        STATE.home = Path(home) if home is not None else None
        STATE.secrets = {str(k): str(v) for k, v in (secrets or {}).items()}
        STATE.secrets_read.clear()
    return ShimBinding()


# ----------------------------------------------------------------------------- modules

_INSTALL_LOCK = threading.Lock()
_saved: dict[str, types.ModuleType | None] = {}
_modules: dict[str, types.ModuleType] = {}


def _build_modules() -> dict[str, types.ModuleType]:
    agent_pkg = types.ModuleType("agent", "Hermes 'agent' package (test shim)")
    agent_pkg.__path__ = []
    plugins_pkg = types.ModuleType("plugins", "Hermes 'plugins' package (test shim)")
    plugins_pkg.__path__ = []

    llm_mod = types.ModuleType("agent.plugin_llm", "agent.plugin_llm (test shim)")
    for name in llm_types.__all__:
        setattr(llm_mod, name, getattr(llm_types, name))
    llm_mod.__all__ = list(llm_types.__all__)

    secret_mod = types.ModuleType("agent.secret_scope", "agent.secret_scope (test shim)")
    secret_mod.get_secret = get_secret
    secret_mod.get_secret_str = get_secret_str
    secret_mod.__all__ = ["get_secret", "get_secret_str"]

    storage_mod = types.ModuleType("plugins.plugin_storage", "plugins.plugin_storage (test shim)")
    storage_mod.plugin_data_dir = plugin_data_dir
    storage_mod.plugin_db = plugin_db
    storage_mod.__all__ = ["plugin_data_dir", "plugin_db"]

    agent_pkg.plugin_llm = llm_mod
    agent_pkg.secret_scope = secret_mod
    plugins_pkg.plugin_storage = storage_mod
    return {
        "agent": agent_pkg,
        "agent.plugin_llm": llm_mod,
        "agent.secret_scope": secret_mod,
        "plugins": plugins_pkg,
        "plugins.plugin_storage": storage_mod,
    }


def installed() -> bool:
    """True while the shim modules are the ones in ``sys.modules``."""
    return bool(_modules) and all(sys.modules.get(n) is m for n, m in _modules.items())


def install() -> list[str]:
    """Install the shim modules (idempotent). Returns the module names."""
    with _INSTALL_LOCK:
        if not _modules:
            _modules.update(_build_modules())
        for name, module in _modules.items():
            if sys.modules.get(name) is not module:
                if name not in _saved:
                    _saved[name] = sys.modules.get(name)
                sys.modules[name] = module
        return list(_modules)


def uninstall() -> None:
    """Restore ``sys.modules`` to its state before :func:`install`."""
    with _INSTALL_LOCK:
        for name, module in _modules.items():
            if sys.modules.get(name) is module:
                previous = _saved.get(name)
                if previous is None:
                    del sys.modules[name]
                else:
                    sys.modules[name] = previous
        _saved.clear()
        STATE.reset()
