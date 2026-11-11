"""Fakes for the setup check tests: a store, an alerter and a ready-made ``SetupEnv``."""

from __future__ import annotations

from harness.plugin_loader import plugin_module

sc = plugin_module("sentinel.agent.setup")
cfg_mod = plugin_module("sentinel.config")
sources_mod = plugin_module("sentinel.sources")


def info(spec, kind="camera", available=True, name="Cam"):
    return sources_mod.SourceInfo(spec=spec, kind=kind, name=name, available=available)


class FakeStore:
    def __init__(self, data_dir, rules=()):
        self.data_dir = data_dir
        self._rules = list(rules)

    def list_rules(self, **_):
        return self._rules


class FakeAlerter:
    def __init__(self, blocked=None, sent=0):
        self._blocked, self._sent = blocked, sent

    def delivery_blocked(self):
        return self._blocked

    def stats(self):
        return {"sent": self._sent, "suppressed": 0, "failed": 0}


def make_env(tmp_path, **over):
    values = dict(
        config=cfg_mod.SentinelConfig(),
        has_llm=True,
        store_getter=lambda: FakeStore(tmp_path),
        engine=None,
        alerter=FakeAlerter(),
        route=None,
        module_available=lambda name: True,
        list_sources=lambda **kw: [info("camera:0"), info("screen:1", "screen", name="Monitor")],
        disk_free_mb=lambda path: 10_000.0,
    )
    values.update(over)
    return sc.SetupEnv(**values)


def run(tmp_path, **over):
    return {c.id: c for c in sc.run_checks(make_env(tmp_path, **over))}
