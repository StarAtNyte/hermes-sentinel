"""The setup check driver: all checks together and a crashing check contained."""

from __future__ import annotations

from harness.plugin_loader import plugin_module
from harness.setup_rig import make_env

sc = plugin_module("sentinel.agent.setup")


def test_healthy_setup_has_only_unverifiable_warnings(tmp_path):
    checks = sc.run_checks(make_env(tmp_path))
    assert [c.id for c in checks] == [
        "deps",
        "injection",
        "vision",
        "sources",
        "default_source",
        "engine",
        "storage",
        "delivery",
        "budget",
        "rules",
    ]
    assert {c.id for c in checks if c.status != "ok"} == {"injection", "vision"}
    assert sc.summarize(checks)["ready"] is True
    assert sc.format_report(checks).splitlines()[-1] == "Ready: yes - 2 items to review"


def test_a_crashing_check_is_contained(tmp_path):
    def boom():
        raise RuntimeError("x")

    checks = {c.id: c for c in sc.run_checks(make_env(tmp_path, store_getter=boom))}
    assert checks["rules"].status == "warn" and "RuntimeError" in checks["rules"].detail
    assert checks["storage"].status == "fail"
