"""Check whether Hermes lets Sentinel inject alerts into the chat."""

from __future__ import annotations

from typing import Any

from ....alerts import GATEWAY_INJECTION_YAML
from ..env import SetupEnv
from ..model import FAIL, OK, WARN, Check

__all__ = ["check_injection"]


def _delivery_verified(alerter: Any) -> bool:
    """Whether a delivery self-test succeeded (`/sentinel setup --test-delivery`)."""
    check = getattr(alerter, "delivery_verified", None)
    return bool(check()) if callable(check) else False


def check_injection(env: SetupEnv) -> Check:
    """FAIL when delivery is blocked, OK once verified or used, else a WARN with the YAML."""
    title = "Alert delivery permission"
    blocked = env.alerter.delivery_blocked() if env.alerter is not None else None
    if blocked:
        return Check(
            "injection",
            title,
            FAIL,
            "Hermes refused to inject Sentinel's alerts into the chat.",
            GATEWAY_INJECTION_YAML,
        )
    if _delivery_verified(env.alerter):
        return Check("injection", title, OK, "The delivery test message was accepted by Hermes.")
    sent = 0
    try:
        sent = int((env.alerter.stats() if env.alerter is not None else {}).get("sent", 0))
    except Exception:  # noqa: BLE001
        sent = 0
    if sent > 0:
        return Check("injection", title, OK, f"{sent} alert(s) delivered since start.")
    return Check(
        "injection",
        title,
        WARN,
        "Cannot be verified from inside a plugin. The classic CLI needs nothing; "
        "every other surface (Desktop, Telegram, Discord...) needs this opt-in "
        "(off by default), otherwise alerts are refused. "
        "Run /sentinel setup --test-delivery to try it (starts one agent turn).",
        GATEWAY_INJECTION_YAML,
    )
