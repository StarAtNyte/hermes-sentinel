"""Credential-safe text for setup check details."""

from __future__ import annotations

import logging
from typing import Any

from ...sources import redact_url
from .env import SetupEnv

logger = logging.getLogger(__name__)

__all__ = ["mask", "named_streams"]


def mask(text: Any) -> str:
    """Strip credentials from any URL-looking word."""
    out = []
    for word in str(text or "").split(" "):
        if "://" in word:
            try:
                word = redact_url(word)
            except (ValueError, TypeError) as exc:
                logger.debug("sentinel: could not redact a URL: %s", type(exc).__name__)
                word = "<url>"
        out.append(word)
    return " ".join(out)[:600]


def named_streams(env: SetupEnv) -> dict[str, str]:
    """The configured ``stream:<name>`` URLs (empty when there is no config)."""
    return dict(getattr(env.config, "stream_urls", None) or {})
