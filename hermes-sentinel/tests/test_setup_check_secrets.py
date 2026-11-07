"""Setup check for camera secrets: ready, missing and waiting for the secret scope."""

from __future__ import annotations

import sys

from harness.plugin_loader import plugin_module
from test_setup_check import FakeStore, cfg_mod, run

rules = plugin_module("sentinel.rules")
sources_mod = plugin_module("sentinel.sources")


class UnscopedSecretError(RuntimeError):
    """Stand-in with the name of Hermes' fail-closed error."""


def _config(url="rtsp://u:${CAM_PW}@10.0.0.5/live"):
    return cfg_mod.SentinelConfig(stream_urls={"door": url})


def test_no_secret_references_means_no_check(tmp_path):
    assert "secrets" not in run(tmp_path)


def test_readable_secret_is_ok_and_values_are_not_shown(tmp_path, shim_binding):
    shim_binding.secrets["CAM_PW"] = "hunter2"
    check = run(tmp_path, config=_config())["secrets"]
    assert check.status == "ok" and "hunter2" not in check.detail


def test_unset_secret_warns_with_the_name(tmp_path, shim_binding):
    check = run(tmp_path, config=_config())["secrets"]
    assert check.status == "warn" and "CAM_PW" in check.detail


def test_whole_url_slot_in_a_rule_source_is_checked(tmp_path, shim_binding):
    spec = rules.RuleSpec(question="q?", source="${SENTINEL_STREAM_URL_1}", id="r").validate()
    check = run(tmp_path, store_getter=lambda: FakeStore(tmp_path, [spec]))["secrets"]
    assert check.status == "warn" and "SENTINEL_STREAM_URL_1" in check.detail


def test_scopeless_thread_reports_waiting_not_failure(tmp_path, shim_binding, monkeypatch):
    def raising(name, default=None):
        raise UnscopedSecretError(name)

    monkeypatch.setattr(sys.modules["agent.secret_scope"], "get_secret", raising)
    sources_mod.clear_cached_secrets()
    check = run(tmp_path, config=_config())["secrets"]
    assert check.status == "warn" and "Waiting for the Hermes secret scope" in check.detail
