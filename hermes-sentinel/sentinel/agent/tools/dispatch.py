"""Wraps a tool body into a Hermes handler that always answers with a JSON string."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ...rules import RuleValidationError
from ...runtime import Sentinel
from ...sources import SourceError, SourceSpecError
from ...storage import StoreError
from .common import ToolError, err

logger = logging.getLogger(__name__)

__all__ = ["wrap_handler"]


def wrap_handler(
    name: str, description: str, body: Callable[..., str], sentinel: Sentinel
) -> Callable[..., str]:
    """A handler ``(args, **kwargs) -> str`` around ``body`` that never raises.

    It primes the Hermes secret scope first, then maps the domain errors to friendly
    ``{"ok": false, "error": ...}`` replies; anything unexpected is logged and reported briefly.
    """

    def handler(args: dict, **kwargs: Any) -> str:
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            return err("arguments must be a JSON object")
        sentinel.prime_secrets()
        try:
            return body(dict(args), **kwargs)
        except ToolError as exc:
            return err(str(exc))
        except SourceSpecError as exc:
            return err(
                f"invalid source: {exc}. Examples: camera:0, screen, rtsp://..., "
                "folder:/path. Use sentinel_sources to list what is available."
            )
        except SourceError as exc:
            return err(f"could not read the source: {exc}")
        except RuleValidationError as exc:
            return err(f"invalid rule: {exc}")
        except StoreError as exc:
            return err(f"Sentinel storage is unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001 - a tool must answer, never crash the turn
            logger.exception("sentinel: tool %s failed", name)
            return err(f"unexpected error ({type(exc).__name__}); see the Hermes log")

    handler.__name__ = f"handle_{name}"
    handler.__doc__ = description
    return handler
