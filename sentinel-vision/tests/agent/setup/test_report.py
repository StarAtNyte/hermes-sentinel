"""Setup report: summary counts, failing titles and the closing verdict."""

from __future__ import annotations

from harness.plugin_loader import plugin_module
from harness.setup_rig import make_env

sc = plugin_module("sentinel.agent.setup")


cfg_mod = plugin_module("sentinel.config")


def _last_line(*statuses: str) -> str:
    checks = [sc.Check(id=str(i), title=str(i), status=s) for i, s in enumerate(statuses)]
    return sc.format_report(checks).splitlines()[-1]


def test_ready_line_wording():
    assert _last_line("ok") == "Ready: yes - nothing to fix"
    assert _last_line("ok", "warn") == "Ready: yes - 1 item to review"
    assert _last_line("warn", "fail") == "Ready: no - 2 things to fix"
    assert _last_line("fail") == "Ready: no - 1 thing to fix"


def test_summary_and_titles(tmp_path):
    checks = sc.run_checks(
        make_env(
            tmp_path,
            module_available=lambda m: False,
            config=cfg_mod.SentinelConfig(vision_calls_per_hour=0),
        )
    )
    assert sc.fail_titles(checks) == ["Python packages", "Vision call caps"]
    s = sc.summarize(checks)
    assert s["ready"] is False and s["failed"] == 2
    assert sc.format_report(checks).splitlines()[-1].startswith("Ready: no - ")
    assert [c.to_dict()["status"] for c in checks][0] == "fail"


def test_next_step_is_suggested_only_when_ready():
    ready = sc.format_report([sc.Check(id="a", title="a", status="ok")])
    blocked = sc.format_report([sc.Check(id="a", title="a", status="fail")])
    assert "Next: tell Hermes what to watch" in ready
    assert "Next:" not in blocked
