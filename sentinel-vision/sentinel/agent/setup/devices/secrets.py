"""Check that every ``${SECRET}`` a stream or rule references can be read."""

from __future__ import annotations

from ....sources import probe_secrets, referenced_secrets
from ..env import SetupEnv
from ..masking import named_streams
from ..model import OK, WARN, Check

__all__ = ["check_secrets"]


def check_secrets(env: SetupEnv) -> Check | None:
    """Can every ``${SECRET}`` a stream or rule references be read? ``None`` if none are used."""
    title = "Camera secrets"
    texts = list(named_streams(env).values())
    if env.store_getter is not None:
        texts += [rule.source for rule in env.store_getter().list_rules(enabled_only=True)]
    status = probe_secrets(referenced_secrets(texts))
    if not status.referenced:
        return None
    if status.missing:
        names = ", ".join(status.missing)
        return Check(
            "secrets",
            title,
            WARN,
            f"Not set: {names}. Cameras that use them stay offline.",
            "Set them in Hermes (plugin settings for sentinel-vision, or the profile .env).",
        )
    if status.waiting:
        names = ", ".join(status.waiting)
        return Check(
            "secrets",
            title,
            WARN,
            f"Waiting for the Hermes secret scope: {names}. This multiplexed gateway only "
            "exposes secrets inside a tool or command call.",
            "Run /sentinel status or any sentinel tool once; the values are then kept in memory "
            "only and the cameras connect by themselves.",
        )
    return Check("secrets", title, OK, f"{len(status.ready)} secret(s) readable, none shown.")
