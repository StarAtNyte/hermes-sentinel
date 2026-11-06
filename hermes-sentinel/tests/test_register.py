"""register() runs on the fake ctx and its registrations match plugin.yaml (catalog rules 6, 11).

``register()`` is exercised under several host configurations (no settings, typical settings,
operator trust granted, bundled source). In every one of them:

* tools / hooks / middleware registered == ``provides_tools`` / ``provides_hooks`` /
  ``provides_middleware`` (what ``hermes plugins validate`` diffs);
* every config key read is declared in ``config_schema``;
* every secret read through ``agent.secret_scope.get_secret`` is declared in ``requires_env``
  (or as a ``type: secret`` ``config_schema`` field);
* the host raised no warnings (unknown hook names, built-in command clashes, shadowed tools).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from harness import hermes_shims
from harness.fake_ctx import FakeCtx
from harness.plugin_loader import MODULE_NAME, PLUGIN_DIR, PLUGIN_NAME, plugin_package

# Same categories `hermes plugins validate` diffs
# (hermes_cli/plugin_validate.py::_check_capabilities).
DECLARED_KEYS = {
    "tools": "provides_tools",
    "hooks": "provides_hooks",
    "middleware": "provides_middleware",
}

_ENV_NAME_CLEAN_RE = re.compile(r"[^A-Z0-9]+")

REGISTER_CONFIGS: dict[str, dict[str, Any]] = {
    "defaults": {},
    "typical_settings": {
        "config": {
            "default_source": "webcam:0",
            "vision_calls_per_hour": 30,
            "confidence_threshold": 0.8,
        }
    },
    "trusted_operator": {
        "entry": {
            "allow_tool_override": True,
            "granted_capabilities": ["tools.override", "llm.model_override"],
            "llm": {"allow_model_override": True, "allowed_models": ["*"]},
        }
    },
    "bundled_with_secrets": {"source": "bundled", "secrets": {"SENTINEL_STREAM_PASSWORD": "x"}},
}


def _manifest() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


def _declared_secret_names(manifest: dict) -> set[str]:
    """``requires_env`` names plus ``type: secret`` config fields (hermes_cli/plugins_settings)."""
    names: set[str] = set()
    for entry in manifest.get("requires_env") or []:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]))
    for key, spec in (manifest.get("config_schema") or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("secret") is True or str(spec.get("type", "")).lower() == "secret":
            env = str(spec.get("env") or "").strip()
            names.add(
                env or _ENV_NAME_CLEAN_RE.sub("_", f"{manifest['name']}_{key}".upper()).strip("_")
            )
    return names


@pytest.fixture(params=sorted(REGISTER_CONFIGS))
def registered(request: pytest.FixtureRequest, plugin_data_root):
    """register() on a FakeCtx built from one of REGISTER_CONFIGS."""
    spec = REGISTER_CONFIGS[request.param]
    with hermes_shims.bind(home=plugin_data_root, secrets=spec.get("secrets")) as binding:
        ctx = FakeCtx(
            plugin_id=PLUGIN_NAME,
            config=spec.get("config"),
            entry=spec.get("entry"),
            source=spec.get("source", "user"),
        )
        try:
            plugin_package().register(ctx)
            yield ctx, binding
        finally:
            ctx.close()


def test_register_runs(loaded_plugin):
    ctx = loaded_plugin.ctx
    assert "sentinel" in ctx.commands
    assert "hermes-sentinel" in ctx.run_command("sentinel", "status")


def test_plugin_is_loaded_under_hermes_module_name(loaded_plugin):
    assert loaded_plugin.module.__name__ == MODULE_NAME
    assert MODULE_NAME == "hermes_plugins.hermes_sentinel"


@pytest.mark.parametrize("kind", sorted(DECLARED_KEYS))
def test_registered_capabilities_match_manifest(registered, kind):
    ctx, _ = registered
    declared = sorted(_manifest().get(DECLARED_KEYS[kind]) or [])
    actual = ctx.registered_capabilities()[kind]
    assert actual == declared, (
        f"{kind}: register() registers {actual} but plugin.yaml {DECLARED_KEYS[kind]} declares "
        f"{declared}. Update plugin.yaml in the same change (catalog rule 6)."
    )


def test_secrets_read_are_declared(registered):
    ctx, binding = registered
    # Exercise the read-only command surface too: handlers may resolve credentials lazily.
    for name in list(ctx.commands):
        ctx.run_command(name, "status")
    undeclared = sorted(set(binding.secrets_read) - _declared_secret_names(_manifest()))
    assert not undeclared, (
        f"get_secret() read {undeclared} but plugin.yaml requires_env/config_schema does not "
        "declare them (catalog rules 6 and 11)."
    )


def test_config_keys_read_are_declared(registered):
    ctx, _ = registered
    schema = _manifest().get("config_schema") or {}
    undeclared = sorted({k.split(".", 1)[0] for k in ctx.config_reads} - set(schema))
    assert not undeclared, f"get_config() read {undeclared}, missing from config_schema"


def test_register_raises_no_host_warnings(registered):
    ctx, _ = registered
    assert list(ctx.warnings) == []


def test_register_is_repeatable_on_a_fresh_ctx(registered):
    """A force-reload registers again on a new context; results must be identical."""
    ctx, _ = registered
    again = FakeCtx(plugin_id=PLUGIN_NAME, config=dict(ctx.config), entry=ctx.entry,
                    source=ctx.source)
    try:
        plugin_package().register(again)
        assert again.registered_capabilities() == ctx.registered_capabilities()
        assert sorted(again.commands) == sorted(ctx.commands)
    finally:
        again.close()


def test_manifest_basics():
    manifest = _manifest()
    assert manifest["name"] == PLUGIN_DIR.name == PLUGIN_NAME
    assert isinstance(manifest["version"], str)  # quoted version (rule 2)
    assert str(manifest["requires_hermes"]).startswith(">=")  # SemVer floor (rule 14)
    for dep in manifest.get("python_dependencies") or []:
        assert ">=" in dep and "<" in dep, f"{dep!r} needs a floor and an upper bound (rule 10)"


def test_declared_secret_names_helper():
    manifest = {
        "name": "hermes-sentinel",
        "requires_env": ["A_KEY", {"name": "B_KEY"}],
        "config_schema": {
            "api_key": {"type": "secret"},
            "token": {"type": "secret", "env": "CUSTOM_TOKEN"},
            "pw": {"secret": True},
            "plain": {"type": "str"},
        },
    }
    assert _declared_secret_names(manifest) == {
        "A_KEY", "B_KEY", "HERMES_SENTINEL_API_KEY", "CUSTOM_TOKEN", "HERMES_SENTINEL_PW",
    }
