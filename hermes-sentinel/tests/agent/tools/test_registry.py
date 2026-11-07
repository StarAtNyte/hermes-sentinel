"""The tool registry: schemas, registration and the never-raise wrapper."""

from __future__ import annotations

import json

import pytest
import yaml
from harness.plugin_loader import PLUGIN_DIR, plugin_module

tools = plugin_module("sentinel.agent.tools")


def test_schemas_are_well_formed_and_match_manifest():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert sorted(manifest["provides_tools"]) == sorted(tools.TOOL_NAMES)
    assert len(tools.TOOL_NAMES) == 9
    import jsonschema

    for name, schema in tools.SCHEMAS.items():
        assert schema["name"] == name and len(schema["description"]) > 20
        params = schema["parameters"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        jsonschema.Draft202012Validator.check_schema(params)
        for prop, spec in params["properties"].items():
            assert "type" in spec or "enum" in spec, (name, prop)
        for req in params.get("required", []):
            assert req in params["properties"]


def test_registered_with_toolset_and_emoji(rig):
    for name in tools.TOOL_NAMES:
        entry = rig.ctx.tools[name]
        assert entry["toolset"] == "sentinel" and entry["emoji"] and entry["description"]


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_every_handler_returns_json_even_for_garbage(rig, name):
    handler = rig.ctx.tools[name]["handler"]
    for args in ({}, {"rule_id": 5, "event_id": "x", "kind": [], "source": {}}, None):
        out = json.loads(handler(args, task_id="t", session_id="s"))
        assert isinstance(out, dict) and "ok" in out
    out = json.loads(handler("not a dict"))
    assert out == {"ok": False, "error": "arguments must be a JSON object"}


def test_unexpected_exceptions_are_reported_briefly(rig, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("secret internals")

    monkeypatch.setattr(rig.sentinel.store, "list_rules", boom)
    out = rig.call("sentinel_rules")
    assert out["ok"] is False and "ZeroDivisionError" in out["error"]
    assert "secret internals" not in out["error"]
