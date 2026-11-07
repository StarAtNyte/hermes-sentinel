"""Test rig: a fully wired Sentinel on a FakeCtx with fake sources and a virtual clock.

``Rig`` builds the same object graph ``register(ctx)`` does (``Sentinel`` + tools + command)
but injects :class:`harness.fake_sources.FakeSource` objects, a :class:`FakeClock` and a
synchronous engine (``threaded=False``), so a whole clip can be "watched" deterministically:

    rig = Rig(fake_ctx, {"camera:7": FakeSource(build_scene("object_removed"), rig.clock)})
    rig.watch("tell me if the mug is taken", source="camera:7")
    rig.pump(30)
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from harness.engine_rig import RELAXED_PACING
from harness.fake_sources import FakeClock, FakeSource, SourceError
from harness.plugin_loader import plugin_module

commands = plugin_module("sentinel.agent.commands")
config_mod = plugin_module("sentinel.config")
engine_mod = plugin_module("sentinel.engine")
runtime = plugin_module("sentinel.runtime")
tools = plugin_module("sentinel.agent.tools")

FAST = engine_mod.EngineSettings(
    capture_interval=0.01,
    confirm_interval=0.2,
    offline_grace=0.5,
    backoff_initial=1.0,
    backoff_max=8.0,
    judge_backoff_initial=1.0,
    judge_backoff_max=4.0,
    scheduler_interval=0.05,
    sync_interval=0.5,
    stop_timeout=5.0,
)


class HoldLastSource(FakeSource):
    """A FakeSource that keeps returning its last frame instead of running out."""

    def read(self):  # type: ignore[override]
        if self.position >= len(self.scene.frames) and not self.disconnected:
            self.position = len(self.scene.frames) - 1
        return super().read()


def verdict(match: bool, confidence: float = 0.9, caption: str = "", reason: str = "") -> dict:
    return {"match": match, "confidence": confidence, "caption": caption, "reason": reason}


class Rig:
    """Sentinel wired onto ``ctx`` with injected sources (see the module docstring)."""

    def __init__(
        self,
        ctx: Any,
        sources: dict[str, Any] | None = None,
        *,
        config: dict[str, Any] | None = None,
        settings: Any = FAST,
        clock: FakeClock | None = None,
        threaded: bool = False,
        wall_clock: Callable[[], float] | None = None,
        use_process_lock: bool = False,
    ) -> None:
        self.ctx = ctx
        self.clock = clock or FakeClock(1000.0)
        self.sources: dict[str, Any] = dict(sources or {})
        self.opened: list[str] = []
        ctx.config.update({**RELAXED_PACING, **(config or {})})
        kwargs: dict[str, Any] = {}
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        self.sentinel = runtime.Sentinel(
            ctx,
            source_factory=self.factory,
            source_key=lambda spec: spec,
            settings=settings,
            clock=time.monotonic if threaded else self.clock,
            threaded=threaded,
            use_process_lock=use_process_lock,
            **kwargs,
        )
        runtime.register_aux_tasks(ctx)
        tools.register_tools(ctx, self.sentinel)
        ctx.register_command("sentinel", handler=commands.SentinelCommand(self.sentinel, "t"))
        ctx.on_unload(self.sentinel.shutdown)

    def factory(self, spec: str) -> Any:
        self.opened.append(spec)
        try:
            return self.sources[spec]
        except KeyError:
            raise SourceError(f"no fake source {spec!r}") from None

    @property
    def engine(self) -> Any:
        return self.sentinel.engine

    def call(self, tool: str, **args: Any) -> dict:
        return json.loads(self.ctx.dispatch_tool(tool, args))

    def watch(self, what: str = "", **args: Any) -> dict:
        if what:
            args["what"] = what
        result = self.call("sentinel_watch", **args)
        assert result["ok"], result
        return result

    def pump(self, steps: int, key: str | None = None) -> None:
        """``steps`` capture iterations of ``key`` (or every source), judging after each."""
        engine = self.engine
        for _ in range(steps):
            for k in [key] if key else engine.source_keys():
                engine.step_source(k)
            engine.run_pending_judgements()
            engine.maintenance()

    def notices(self, needle: str = "") -> list[str]:
        return [
            m.content
            for m in self.ctx.injected
            if m.content.startswith("[Sentinel notice]") and needle in m.content
        ]

    def alerts(self) -> list[str]:
        return [m.content for m in self.ctx.injected if m.content.startswith("[Sentinel alert]")]
