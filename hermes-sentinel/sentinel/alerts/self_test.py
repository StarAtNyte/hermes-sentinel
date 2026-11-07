"""Delivery self-test: prove that Hermes accepts Sentinel's injected messages.

A plugin cannot read its own ``allow_gateway_injection`` flag, so the only honest check is to
send one small message and look at the boolean ``inject_message`` returns. The message starts an
agent turn, so the test is opt-in and never runs automatically.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .formatting import GATEWAY_INJECTION_YAML

TEST_MESSAGE = (
    "[Sentinel delivery test] Sentinel is checking that its alerts can reach this chat. "
    "Reply with the single word: ok"
)


@dataclass(frozen=True)
class DeliveryTestResult:
    """Outcome of one self-test: whether Hermes accepted the message, and by which route."""

    delivered: bool
    via: str
    detail: str = ""


def run_delivery_test(alerter: Any, origin: Mapping[str, Any] | None) -> DeliveryTestResult:
    """Send the self-test message to ``origin`` through ``alerter``. Never raises."""
    delivery = alerter.send_test(TEST_MESSAGE, origin)
    return DeliveryTestResult(delivery.ok, delivery.via, delivery.detail)


def format_delivery_test(result: DeliveryTestResult) -> str:
    """Human text for a result; a refusal includes the exact config fix."""
    if result.delivered:
        return (
            f"Delivery test: delivered (route {result.via}). Hermes accepted the message and "
            "the agent should answer 'ok' shortly; alerts can reach this chat."
        )
    if result.detail.startswith("this chat has no known route"):
        return f"Delivery test: not sent - {result.detail}."
    return (
        "Delivery test: BLOCKED. Hermes refused to inject the message on every route "
        "(allow_gateway_injection is off, or no live gateway/session is available). "
        "Outside the classic CLI add this to config.yaml and restart:\n" + GATEWAY_INJECTION_YAML
    )
