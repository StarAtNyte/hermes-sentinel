"""``register(ctx)`` wires tools, aux tasks, the command and the unload hook."""

from __future__ import annotations

import threading
import time

from harness.fake_ctx import FakeCtx
from harness.plugin_loader import PLUGIN_NAME, plugin_package


def test_register_wires_tools_tasks_command_and_unload(loaded_plugin):
    ctx = loaded_plugin.ctx
    assert sorted(ctx.tools) == sorted(
        [
            "sentinel_sources",
            "sentinel_watch",
            "sentinel_rules",
            "sentinel_update",
            "sentinel_look",
            "sentinel_events",
            "sentinel_feedback",
            "sentinel_teach",
            "sentinel_setup_check",
        ]
    )
    assert set(ctx.aux_tasks) == {"sentinel_vision", "sentinel_rules"}
    assert ctx.commands["sentinel"]["args_hint"].startswith("[setup|status")
    engine_store = loaded_plugin.ctx.run_command("sentinel", "status")
    assert "idle" in engine_store
    ctx.unload()  # the on_unload callback (Sentinel.shutdown) runs cleanly
    assert ctx.unload_errors == []
    assert list(ctx.warnings) == []


def test_register_boots_watcher_for_existing_rules_and_unload_stops_it(
    shim_binding, tmp_path, monkeypatch
):
    from harness.plugin_loader import plugin_module

    monkeypatch.setattr(plugin_module("sentinel.runtime"), "BOOT_DELAY_SECONDS", 0.0)

    store_mod = plugin_module("sentinel.storage")
    rules = plugin_module("sentinel.rules")
    folder = tmp_path / "frames"
    folder.mkdir()
    store = store_mod.Store()
    store.upsert_rule(rules.RuleSpec(question="q?", source=f"folder:{folder}").validate())
    store.close()
    ctx = FakeCtx(plugin_id=PLUGIN_NAME)
    try:
        plugin_package().register(ctx)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            t.name.startswith("sentinel-capture") for t in threading.enumerate()
        ):
            time.sleep(0.01)
        assert any(t.name.startswith("sentinel-capture") for t in threading.enumerate())
        assert "watching" in ctx.run_command("sentinel", "status")
    finally:
        ctx.close()
    assert not [t for t in threading.enumerate() if t.name.startswith("sentinel-")]
    assert ctx.unload_errors == []
