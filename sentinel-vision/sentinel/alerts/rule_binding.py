"""Binding rules to a chat route: planning (pure) and applying (store + engine)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .chat_hints import has_route
from .routing import clean_value

SESSION_KEYS = ("session_id", "surface")


@dataclass(frozen=True)
class HerePlan:
    """What ``/sentinel here`` should do: bind ``rules``, or just answer with ``reply``."""

    rules: tuple[Any, ...] = ()
    reply: str = ""


def plan_here(rules: Sequence[Any], target: str, session_id: str) -> HerePlan:
    """Decide which of the visible ``rules`` ``/sentinel here <target>`` binds.

    ``target``: a rule id, ``all``, or empty. Empty binds this session's rules; with none, it
    binds the only rule that still lacks a route, or lists the candidates with the exact
    command for each.
    """
    if target and target.lower() != "all":
        chosen = tuple(r for r in rules if r.id == target)
        return HerePlan(chosen, "" if chosen else f"No rule {target!r}. Try /sentinel list.")
    if target:
        return HerePlan(tuple(rules), "" if rules else "No rules to move. Try /sentinel list.")
    mine = tuple(r for r in rules if session_id and r.origin.get("session_id") == session_id)
    if mine:
        return HerePlan(mine)
    return _plan_unrouted(tuple(r for r in rules if not has_route(r.origin)))


def _plan_unrouted(unrouted: tuple[Any, ...]) -> HerePlan:
    if len(unrouted) == 1:
        return HerePlan(unrouted)
    if unrouted:
        commands = "\n".join(f"  /sentinel here {r.id}   ({_label(r)})" for r in unrouted)
        return HerePlan(
            reply="Several rules have no destination yet. Pick the ones for this chat:\n"
            + commands
            + "\nOr /sentinel here all for every rule above."
        )
    return HerePlan(
        reply="Alerts for rules created in this session from now on will come here. "
        "Use /sentinel here all to move existing rules too."
    )


def _label(spec: Any) -> str:
    return str(spec.text or spec.question)[:50]


def rebound_origin(origin: Mapping[str, Any], caller: Mapping[str, Any]) -> dict[str, Any]:
    """The origin a rule gets when ``caller`` resumes it: the caller's live session.

    The old origin is kept when the caller does not identify a session, so a resume from an
    anonymous context never erases a working route.
    """
    if not (clean_value(caller.get("session_id")) or clean_value(caller.get("session_key"))):
        return dict(origin)
    return dict(caller)


def bind_rules(
    store: Any, engine: Any, specs: Sequence[Any], route: Mapping[str, str]
) -> list[str]:
    """Save each rule with ``route`` merged into its origin; returns the bound ids."""
    bound = []
    for spec in specs:
        origin = {k: v for k, v in spec.origin.items() if k in SESSION_KEYS}
        origin.update(route)
        saved = store.upsert_rule(spec.with_changes(origin=origin))
        if engine is not None:
            engine.update_rule(saved)
        bound.append(saved.id)
    return bound


def adopt_session_rules(
    store: Any, engine: Any, rules: Sequence[Any], session_id: str, route: Mapping[str, str]
) -> list[str]:
    """Give this session's own route-less rules the route just learned (same session only)."""
    if not route.get("session_key") or not clean_value(session_id):
        return []
    mine = [
        r for r in rules if r.origin.get("session_id") == session_id and not has_route(r.origin)
    ]
    return bind_rules(store, engine, mine, route)
