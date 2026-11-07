"""``/sentinel setup``: the readiness check, optionally with the delivery self-test."""

from __future__ import annotations

from typing import Any

from ...alerts import format_delivery_test, run_delivery_test
from ...runtime import Sentinel
from ..setup import SetupEnv, format_report, run_checks
from .common import caller_origin

__all__ = ["run_setup"]


def run_setup(sentinel: Sentinel, kwargs: dict[str, Any], *, test_delivery: bool = False) -> str:
    """Run the setup check (probes devices briefly; no network, no vision calls).

    ``test_delivery`` also sends one small message to this chat (it starts an agent
    turn, so it is opt-in) and reports whether Hermes accepted it.
    """
    result = (
        run_delivery_test(sentinel.alerter, caller_origin(sentinel, kwargs))
        if test_delivery
        else None
    )
    report = format_report(run_checks(SetupEnv.from_sentinel(sentinel, kwargs)))
    return report if result is None else f"{report}\n{format_delivery_test(result)}"
