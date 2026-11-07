"""The ``/sentinel`` slash command: parses the subcommand and hands it to its module.

Handlers return text (they run in the gateway, so ``print`` would go nowhere) and never raise.
``test`` makes one vision call (counted against the budget); everything else is local.
"""

from __future__ import annotations

import logging
from typing import Any

from ...runtime import Sentinel
from .budget import describe_budget
from .common import USAGE, adopt_route, caller_origin, remember_route
from .here import bind_here
from .pause import set_enabled
from .rule_probe import probe_rule
from .setup import run_setup
from .status import list_rules_text, status_text

logger = logging.getLogger(__name__)

__all__ = ["SentinelCommand"]

_SETUP_WORDS = ("setup", "check", "doctor")
_DELIVERY_FLAGS = ("--test-delivery", "test-delivery")


class SentinelCommand:
    """Callable handler for ``ctx.register_command("sentinel", ...)``."""

    def __init__(self, sentinel: Sentinel, version: str = "") -> None:
        """Bind the command to one Sentinel; ``version`` is shown in the status header."""
        self.s = sentinel
        self.version = version

    def __call__(self, raw_args: str = "", **kwargs: Any) -> str:
        """Run ``/sentinel <subcommand> [arg]`` and return the reply text."""
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else "status"
        arg = parts[1] if len(parts) > 1 else ""
        test_delivery = sub in _SETUP_WORDS and any(p.lower() in _DELIVERY_FLAGS for p in parts[1:])
        self.s.prime_secrets()
        try:
            route = remember_route(self.s, kwargs)
            if sub != "here":
                adopt_route(self.s, route, kwargs)
            caller = caller_origin(self.s, kwargs)
            return self._dispatch(sub, arg, route, kwargs, caller, test_delivery)
        except Exception as exc:  # noqa: BLE001 - a command must answer, not crash
            logger.exception("sentinel: /sentinel %s failed", sub)
            return f"Sentinel error: {exc}"

    def _dispatch(
        self,
        sub: str,
        arg: str,
        route: dict[str, str],
        kwargs: dict[str, Any],
        caller: dict[str, Any],
        test_delivery: bool,
    ) -> str:
        if sub == "here":
            return self.here(arg, route, kwargs, caller)
        if sub in _SETUP_WORDS:
            return self.setup(kwargs, test_delivery=test_delivery)
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

    def here(
        self,
        target: str,
        route: dict[str, str],
        kwargs: dict[str, Any],
        caller: dict[str, Any] | None = None,
    ) -> str:
        """Send alerts of this session's rules (or ``<id>`` / ``all``) to this chat."""
        return bind_here(self.s, target, route, kwargs, caller)

    def setup(self, kwargs: dict[str, Any], *, test_delivery: bool = False) -> str:
        """Run the setup check; ``test_delivery`` also sends the one-message delivery test."""
        return run_setup(self.s, kwargs, test_delivery=test_delivery)

    def status(self, caller: dict[str, Any] | None = None) -> str:
        """Watcher state, sources, alert delivery, budget and config problems."""
        return status_text(self.s, self.version, caller)

    def list_rules(self, caller: dict[str, Any] | None = None) -> str:
        """One line per rule the caller can see."""
        return list_rules_text(self.s, caller)

    def set_enabled(self, rule_id: str, enabled: bool, caller: dict[str, Any] | None = None) -> str:
        """Pause (``enabled=False``) or resume one rule."""
        return set_enabled(self.s, rule_id, enabled, caller)

    def test(self, rule_id: str, caller: dict[str, Any] | None = None) -> str:
        """Judge one live frame against a rule (one vision call)."""
        return probe_rule(self.s, rule_id, caller)

    def budget(self) -> str:
        """The vision call budget as one line."""
        return describe_budget(self.s)
