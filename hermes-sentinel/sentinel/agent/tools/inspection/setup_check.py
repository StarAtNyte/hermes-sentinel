"""``sentinel_setup_check``: the readiness check, optionally with the delivery self-test."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ....alerts import format_delivery_test, run_delivery_test
from ...setup import SetupEnv, format_report, run_checks, summarize
from ..common import ToolContext, ToolSpec, ok, schema

__all__ = ["SPEC"]

SCHEMA = schema(
    "sentinel_setup_check",
    "Check whether Sentinel is ready to watch and alert: Python packages, cameras and screens, "
    "default source, storage, alert delivery permission, vision model, caps and rules. Returns "
    "an ordered list of checks (ok / warn / fail, with the exact fix) and a ready flag. Use it "
    "when the user is setting up, or when watching or alerts do not work.",
    {
        "test_delivery": {
            "type": "boolean",
            "description": "Also send one small test message to this chat and report whether "
            "Hermes accepted it (starts one agent turn; only when the user asks to test "
            "alert delivery).",
        }
    },
)


def run(context: ToolContext, args: dict[str, Any], kwargs: Mapping[str, Any]) -> str:
    """Run every setup check; with ``test_delivery`` also send the one-message delivery test."""
    sentinel = context.sentinel
    test = None
    if args.get("test_delivery") is True:
        test = run_delivery_test(sentinel.alerter, context.caller(kwargs))
    checks = run_checks(SetupEnv.from_sentinel(sentinel, kwargs))
    extra: dict[str, Any] = {}
    report = format_report(checks)
    if test is not None:
        extra["delivery_test"] = {"delivered": test.delivered, "via": test.via}
        report += "\n" + format_delivery_test(test)
    return ok(checks=[c.to_dict() for c in checks], report=report, **summarize(checks), **extra)


SPEC = ToolSpec("sentinel_setup_check", SCHEMA, "\U0001fa7a", run)
