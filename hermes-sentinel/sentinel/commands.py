"""``/sentinel`` slash command: ``setup | status | list | pause | resume | test | budget | here``.

Handlers return text (they run in the gateway, so ``print`` would go nowhere) and never raise.
``test`` makes one vision call (counted against the budget); everything else is local.
"""

from __future__ import annotations

import logging
from typing import Any

from .alerts import GATEWAY_INJECTION_YAML, capture_origin, is_chat_origin, same_chat
from .runtime import Sentinel
from .setup_check import SetupEnv, format_report, run_checks

logger = logging.getLogger(__name__)

__all__ = ["ARGS_HINT", "USAGE", "SentinelCommand"]

ARGS_HINT = "[setup|status|list|pause <id>|resume <id>|test <id>|budget|here [<id>|all]]"
USAGE = (
    "Usage: /sentinel setup | status | list | pause <rule_id> | resume <rule_id> | "
    "test <rule_id> | "
    "budget | here [<rule_id>|all]"
)


def _yesno(value: Any) -> str:
    return {True: "online", False: "offline", None: "connecting"}.get(value, str(value))


class SentinelCommand:
    """Callable handler for ``ctx.register_command("sentinel", ...)``."""

    def __init__(self, sentinel: Sentinel, version: str = "") -> None:
        self.s = sentinel
        self.version = version

    def __call__(self, raw_args: str = "", **kwargs: Any) -> str:
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else "status"
        arg = parts[1] if len(parts) > 1 else ""
        try:
            route = self._remember(kwargs)
            caller = self._caller(kwargs)
            if sub == "here":
                return self.here(arg, route, kwargs, caller)
            if sub in ("setup", "check", "doctor"):
                return self.setup(kwargs)
            if sub in ("status", "st"):
                return self.status(caller)
            if sub in ("list", "ls", "rules"):
                return self.list_rules(caller)
            if sub in ("pause", "resume"):
                return self.set_enabled(arg, sub == "resume", caller)
            if sub == "test":
                return self.test(arg, caller)
            if sub == "budget":
                return self.budget()
            if sub in ("help", "-h", "--help"):
                return USAGE
            return f"Unknown subcommand {sub!r}. {USAGE}"
        except Exception as exc:  # noqa: BLE001 - a command must answer, not crash
            logger.exception("sentinel: /sentinel %s failed", sub)
            return f"Sentinel error: {exc}"

    def _remember(self, kwargs: dict[str, Any]) -> dict[str, str]:
        """Learn this chat's route (documented command keywords) for later rules."""
        try:
            return self.s.routes.remember(kwargs)
        except Exception as exc:  # noqa: BLE001 - routing memory is best effort
            logger.debug("sentinel: could not remember route: %s", exc)
            return {}

    def _caller(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Who is typing: the command keywords, the session surface and any known route."""
        try:
            return capture_origin(kwargs, self.s.ctx, routes=self.s.routes)
        except Exception:  # noqa: BLE001
            return {}

    def _visible_rules(self, caller: dict[str, Any]) -> list[Any]:
        """Rules the caller may see: all for local sessions, only its own in a messaging chat."""
        return [r for r in self.s.store.list_rules() if same_chat(r.origin, caller)]

    # -- subcommands

    def here(self, target: str, route: dict[str, str], kwargs: dict[str, Any],
             caller: dict[str, Any] | None = None) -> str:
        """Send alerts of this session's rules (or ``<id>`` / ``all``) to this chat.

        In a messaging chat only rules that already belong to that chat can be moved; a
        local session may move any rule.
        """
        if not route.get("session_key"):
            return ("This session did not identify itself (no session key), so alerts cannot "
                    "be routed here.")
        caller = caller if caller is not None else {}
        session_id = str(kwargs.get("session_id") or route.get("session_key") or "")
        rules = self._visible_rules(caller)
        if target and target.lower() != "all":
            rules = [r for r in rules if r.id == target]
            if not rules:
                return f"No rule {target!r}. Try /sentinel list."
        elif not target:
            rules = [r for r in rules if session_id and r.origin.get("session_id") == session_id]
        bound = []
        for spec in rules:
            origin = {k: v for k, v in spec.origin.items() if k in ("session_id", "surface")}
            origin.update(route)
            saved = self.s.store.upsert_rule(spec.with_changes(origin=origin))
            engine = self.s.engine_if_built
            if engine is not None:
                engine.update_rule(saved)
            bound.append(saved.id)
        if not bound:
            return ("Alerts for rules created in this session from now on will come here. "
                    "Use /sentinel here all to move existing rules too.")
        return f"Alerts for {', '.join(bound)} will now come to this chat."

    def setup(self, kwargs: dict[str, Any]) -> str:
        """Run the setup check (probes devices briefly; no network, no vision calls)."""
        env = SetupEnv.from_sentinel(self.s, kwargs)
        return format_report(run_checks(env))

    def status(self, caller: dict[str, Any] | None = None) -> str:
        head = f"hermes-sentinel {self.version}".strip()
        try:
            rules = self._visible_rules(caller or {})
        except Exception as exc:  # noqa: BLE001
            return f"{head}: storage unavailable ({exc})."
        scoped = is_chat_origin(caller)
        visible = {r.id for r in rules}
        active = sum(1 for r in rules if r.enabled)
        engine = self.s.engine_if_built
        if engine is not None and engine.running:
            state = "watching"
        elif engine is not None and engine.standing_by:
            state = "standing by (another Hermes process is watching)"
        else:
            state = "idle"
        lines = [f"{head}: {state}; {active} active / {len(rules)} rules."]
        if engine is not None:
            st = engine.status()
            for src in st["sources"]:
                if scoped and not visible.intersection(src["rules"]):
                    continue
                if scoped:
                    src = {**src, "rules": [r for r in src["rules"] if r in visible]}
                extra = f" ({src['error']})" if src["error"] else ""
                lines.append(f"  source {src['source']}: {_yesno(src['online'])}{extra}, "
                             f"{src['frames']} frames, rules {', '.join(src['rules'])}")
            alerts = st["alerts"]
            if not scoped:
                lines.append(f"  events fired: {st['events_fired']}; alerts sent "
                             f"{alerts['sent']}, rate-limited {alerts['suppressed']}, "
                             f"failed {alerts['failed']}")
            if st["last_error"] and not scoped:
                lines.append(f"  last error: {st['last_error']}")
        blocked = self.s.alerter.delivery_blocked()
        if blocked:
            lines.append("  " + blocked.replace("\n", "\n  "))
        else:
            lines.append("  Alerts are injected into the chat; outside the classic CLI Hermes "
                         "needs this in config.yaml (off by default):\n  "
                         + GATEWAY_INJECTION_YAML.replace("\n", "\n  "))
        try:
            lines.append("  " + self.s.budget.status().describe())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  budget unavailable: {exc}")
        for problem in self.s.config.problems:
            lines.append(f"  config: {problem}")
        return "\n".join(lines)

    def list_rules(self, caller: dict[str, Any] | None = None) -> str:
        rules = self._visible_rules(caller or {})
        if not rules:
            return "No watch rules yet. Ask Hermes to watch something, e.g. \"watch my webcam " \
                   "and tell me if someone takes my mug\"."
        engine = self.s.engine_if_built
        lines = []
        for spec in rules:
            status = engine.rule_status(spec.id) if engine is not None else None
            state = "paused" if not spec.enabled else (status or {}).get("state", "not running")
            last = self.s.store.last_event(spec.id)
            last_text = f", last event #{last.id} {last.caption[:60]}" if last else ""
            lines.append(f"{spec.id} [{state}] {spec.source}: {spec.text or spec.question}"
                         f"{last_text}")
        return "\n".join(lines)

    def set_enabled(self, rule_id: str, enabled: bool, caller: dict[str, Any] | None = None) -> str:
        if not rule_id:
            return USAGE
        spec = self.s.store.get_rule(rule_id)
        if spec is not None and not same_chat(spec.origin, caller or {}):
            spec = None
        if spec is None:
            return f"No rule {rule_id!r}. Try /sentinel list."
        saved = self.s.store.upsert_rule(spec.with_changes(enabled=enabled))
        if enabled:
            self.s.engine.add_rule(saved)
        elif self.s.engine_if_built is not None:
            self.s.engine.update_rule(saved)
        return f"{'Resumed' if enabled else 'Paused'} rule {rule_id}."

    def test(self, rule_id: str, caller: dict[str, Any] | None = None) -> str:
        if not rule_id:
            return USAGE
        spec = self.s.store.get_rule(rule_id)
        if spec is not None and not same_chat(spec.origin, caller or {}):
            return f"Test of {rule_id} failed: no such rule"
        result = self.s.engine.test_rule(rule_id)
        if not result.get("ok"):
            return f"Test of {rule_id} failed: {result.get('error')}"
        answer = "yes" if result["match"] else "no"
        counts = "would count as evidence" if result.get("would_count") else "would not count"
        return (f"{rule_id}: {result['question']} -> {answer} (confidence "
                f"{result['confidence']:.2f}; {counts}). {result.get('caption') or ''}").strip()

    def budget(self) -> str:
        return self.s.budget.status().describe()
