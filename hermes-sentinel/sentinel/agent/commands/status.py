"""``/sentinel status`` and ``/sentinel list``: what is being watched and how it is doing."""

from __future__ import annotations

from typing import Any

from ...alerts import GATEWAY_INJECTION_YAML, is_chat_origin, route_hint
from ...runtime import Sentinel
from .common import visible_rules

__all__ = ["list_rules_text", "status_text"]

_ONLINE_WORDS = {True: "online", False: "offline", None: "connecting"}


def _engine_state(engine: Any) -> str:
    if engine is not None and engine.running:
        return "watching"
    if engine is not None and engine.standing_by:
        return "standing by (another Hermes process is watching)"
    return "idle"


def _source_lines(engine_status: dict[str, Any], scoped: bool, visible: set[str]) -> list[str]:
    lines = []
    for src in engine_status["sources"]:
        if scoped and not visible.intersection(src["rules"]):
            continue
        if scoped:
            src = {**src, "rules": [r for r in src["rules"] if r in visible]}
        extra = f" ({src['error']})" if src["error"] else ""
        online = _ONLINE_WORDS.get(src["online"], str(src["online"]))
        lines.append(
            f"  source {src['source']}: {online}{extra}, "
            f"{src['frames']} frames, rules {', '.join(src['rules'])}"
        )
    return lines


def _engine_lines(engine: Any, scoped: bool, visible: set[str]) -> list[str]:
    st = engine.status()
    lines = _source_lines(st, scoped, visible)
    alerts = st["alerts"]
    if not scoped:
        lines.append(
            f"  events fired: {st['events_fired']}; alerts sent "
            f"{alerts['sent']}, rate-limited {alerts['suppressed']}, "
            f"failed {alerts['failed']}"
        )
    if st["last_error"] and not scoped:
        lines.append(f"  last error: {st['last_error']}")
    return lines


def _pacing_lines(sentinel: Sentinel, engine: Any, rules: list[Any]) -> list[str]:
    """The per-rule judge limits, then each visible rule that has used some of its hour."""
    cfg = sentinel.config
    lines = [
        f"  per-rule limits: a judge call at most every {cfg.rule_min_interval_seconds:g} s "
        f"(every {cfg.latched_min_interval_seconds:g} s while latched) "
        f"and {cfg.rule_calls_per_hour} per hour"
    ]
    for spec in rules:
        status = engine.rule_status(spec.id)
        if status and status["judge_calls_hour"]:
            lines.append(
                f"  rule {spec.id}: {status['judge_calls_hour']}/{status['judge_cap_hour']} "
                "judge calls this hour"
            )
    return lines


def _delivery_lines(sentinel: Sentinel) -> list[str]:
    blocked = sentinel.alerter.delivery_blocked()
    if blocked:
        return ["  " + blocked.replace("\n", "\n  ")]
    return [
        "  Alerts are injected into the chat; outside the classic CLI Hermes "
        "needs this in config.yaml (off by default):\n  "
        + GATEWAY_INJECTION_YAML.replace("\n", "\n  ")
    ]


def _budget_line(sentinel: Sentinel) -> str:
    try:
        return "  " + sentinel.budget.status().describe()
    except Exception as exc:  # noqa: BLE001
        return f"  budget unavailable: {exc}"


def status_text(sentinel: Sentinel, version: str, caller: dict[str, Any] | None = None) -> str:
    """The status block: watcher state, sources, alert delivery, budget and config problems."""
    head = f"hermes-sentinel {version}".strip()
    try:
        rules = visible_rules(sentinel, caller or {})
    except Exception as exc:  # noqa: BLE001
        return f"{head}: storage unavailable ({exc})."
    scoped = is_chat_origin(caller)
    engine = sentinel.engine_if_built
    active = sum(1 for r in rules if r.enabled)
    lines = [f"{head}: {_engine_state(engine)}; {active} active / {len(rules)} rules."]
    if engine is not None:
        lines += _engine_lines(engine, scoped, {r.id for r in rules})
        lines += _pacing_lines(sentinel, engine, rules)
    lines += _delivery_lines(sentinel)
    lines.append(_budget_line(sentinel))
    lines += [f"  config: {problem}" for problem in sentinel.config.problems]
    return "\n".join(lines)


def list_rules_text(sentinel: Sentinel, caller: dict[str, Any] | None = None) -> str:
    """One line per visible rule with its state and last event."""
    rules = visible_rules(sentinel, caller or {})
    if not rules:
        return (
            'No watch rules yet. Ask Hermes to watch something, e.g. "watch my webcam '
            'and tell me if someone takes my mug".'
        )
    engine = sentinel.engine_if_built
    lines = []
    for spec in rules:
        status = engine.rule_status(spec.id) if engine is not None else None
        state = "paused" if not spec.enabled else (status or {}).get("state", "not running")
        last = sentinel.store.last_event(spec.id)
        last_text = f", last event #{last.id} {last.caption[:60]}" if last else ""
        lines.append(f"{spec.id} [{state}] {spec.source}: {spec.text or spec.question}{last_text}")
        hint = route_hint(spec.origin, caller or {})
        if hint:
            lines.append(f"  note: {hint}")
    return "\n".join(lines)
