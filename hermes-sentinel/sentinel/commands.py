"""``/sentinel`` slash command: ``status | list | pause <id> | resume <id> | test <id> | budget``.

Handlers return text (they run in the gateway, so ``print`` would go nowhere) and never raise.
``test`` makes one vision call (counted against the budget); everything else is local.
"""

from __future__ import annotations

import logging
from typing import Any

from .runtime import Sentinel

logger = logging.getLogger(__name__)

__all__ = ["ARGS_HINT", "USAGE", "SentinelCommand"]

ARGS_HINT = "[status|list|pause <id>|resume <id>|test <id>|budget|here [<id>|all]]"
USAGE = (
    "Usage: /sentinel status | list | pause <rule_id> | resume <rule_id> | test <rule_id> | "
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
            if sub == "here":
                return self.here(arg, route, kwargs)
            if sub in ("status", "st"):
                return self.status()
            if sub in ("list", "ls", "rules"):
                return self.list_rules()
            if sub in ("pause", "resume"):
                return self.set_enabled(arg, sub == "resume")
            if sub == "test":
                return self.test(arg)
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

    # -- subcommands

    def here(self, target: str, route: dict[str, str], kwargs: dict[str, Any]) -> str:
        """Send alerts of this session's rules (or ``<id>`` / ``all``) to this chat."""
        if not route.get("session_key"):
            return ("This chat did not identify itself (no session key), so alerts cannot be "
                    "routed here. Local sessions need no setup.")
        session_id = str(kwargs.get("session_id") or "")
        rules = self.s.store.list_rules()
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
            return ("Alerts for rules created in this chat from now on will come here. "
                    "Use /sentinel here all to move existing rules too.")
        return f"Alerts for {', '.join(bound)} will now come to this chat."

    def status(self) -> str:
        head = f"hermes-sentinel {self.version}".strip()
        try:
            rules = self.s.store.list_rules()
        except Exception as exc:  # noqa: BLE001
            return f"{head}: storage unavailable ({exc})."
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
                extra = f" ({src['error']})" if src["error"] else ""
                lines.append(f"  source {src['source']}: {_yesno(src['online'])}{extra}, "
                             f"{src['frames']} frames, rules {', '.join(src['rules'])}")
            alerts = st["alerts"]
            lines.append(f"  events fired: {st['events_fired']}; alerts sent {alerts['sent']}, "
                         f"rate-limited {alerts['suppressed']}, failed {alerts['failed']}")
            if st["last_error"]:
                lines.append(f"  last error: {st['last_error']}")
        try:
            lines.append("  " + self.s.budget.status().describe())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  budget unavailable: {exc}")
        for problem in self.s.config.problems:
            lines.append(f"  config: {problem}")
        return "\n".join(lines)

    def list_rules(self) -> str:
        rules = self.s.store.list_rules()
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

    def set_enabled(self, rule_id: str, enabled: bool) -> str:
        if not rule_id:
            return USAGE
        spec = self.s.store.get_rule(rule_id)
        if spec is None:
            return f"No rule {rule_id!r}. Try /sentinel list."
        saved = self.s.store.upsert_rule(spec.with_changes(enabled=enabled))
        if enabled:
            self.s.engine.add_rule(saved)
        elif self.s.engine_if_built is not None:
            self.s.engine.update_rule(saved)
        return f"{'Resumed' if enabled else 'Paused'} rule {rule_id}."

    def test(self, rule_id: str) -> str:
        if not rule_id:
            return USAGE
        result = self.s.engine.test_rule(rule_id)
        if not result.get("ok"):
            return f"Test of {rule_id} failed: {result.get('error')}"
        answer = "yes" if result["match"] else "no"
        counts = "would count as evidence" if result.get("would_count") else "would not count"
        return (f"{rule_id}: {result['question']} -> {answer} (confidence "
                f"{result['confidence']:.2f}; {counts}). {result.get('caption') or ''}").strip()

    def budget(self) -> str:
        return self.s.budget.status().describe()
