"""HTTP plumbing shared by the routes: errors, JSON replies, worker threads, input checks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("hermes_sentinel.dashboard")

__all__ = [
    "MAX_CAPTION",
    "MAX_SOURCE",
    "MAX_TEXT",
    "NO_STORE",
    "ApiError",
    "check_event_id",
    "check_rule_id",
    "fail",
    "json_reply",
    "opt_str",
    "positive_int",
    "read_body",
    "reply",
    "run_blocking",
]

MAX_BODY_BYTES = 16 * 1024
MAX_TEXT = 2000
MAX_SOURCE = 2048
MAX_CAPTION = 600
NO_STORE = {"Cache-Control": "no-store"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INT_RE = re.compile(r"^[0-9]{1,12}$")


class ApiError(Exception):
    """A failure with the HTTP status the client should see."""

    def __init__(self, status: int, message: str) -> None:
        """Keep the HTTP status and the client-facing message."""
        super().__init__(message)
        self.status = status
        self.message = message


def fail(status: int, message: str) -> JSONResponse:
    """The ``{ok: false, error}`` reply for ``status``."""
    return JSONResponse({"ok": False, "error": message}, status_code=status, headers=NO_STORE)


def json_reply(payload: Any, status: int = 200) -> JSONResponse:
    """``payload`` as an uncached JSON reply."""
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def reply(result: Any) -> Response:
    """A response from a worker result: failures already are responses, data becomes JSON."""
    return result if isinstance(result, Response) else json_reply(result)


async def run_blocking(fn: Any, *args: Any) -> Any:
    """Run ``fn`` in a worker thread; map known failures to ``{ok:false,error}`` responses."""
    try:
        return await asyncio.to_thread(fn, *args)
    except ApiError as exc:
        return fail(exc.status, exc.message)
    except Exception as exc:  # noqa: BLE001 - broad: route boundary maps every failure to an HTTP error
        name = type(exc).__name__
        if name == "StoreError":
            logger.warning("sentinel dashboard: %s hit a storage error: %s", fn.__name__, exc)
            return fail(503, "Sentinel storage is unavailable")
        logger.exception("sentinel dashboard: %s failed", fn.__name__)
        return fail(500, f"unexpected error ({name})")


async def read_body(request: Request) -> dict[str, Any]:
    """The request's JSON object body (empty dict when blank); bounded in size."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(413, "request body too large")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError(400, "body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def opt_str(data: dict[str, Any], key: str, limit: int) -> str:
    """An optional stripped string field (``""`` when absent), bounded by ``limit``."""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"'{key}' must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ApiError(400, f"'{key}' is too long (max {limit} characters)")
    return value


def check_rule_id(rule_id: str) -> str:
    """``rule_id`` when it has the shape of a rule id, else a 400."""
    if not _ID_RE.match(rule_id or ""):
        raise ApiError(400, "invalid rule id")
    return rule_id


def check_event_id(event_id: str) -> int:
    """``event_id`` as an int when it is all digits, else a 400."""
    if not _INT_RE.match(event_id or ""):
        raise ApiError(400, "invalid event id")
    return int(event_id)


def positive_int(text: str) -> int | None:
    """``text`` as a positive int, ``None`` when it is not one."""
    if _INT_RE.match(text) and int(text) >= 1:
        return int(text)
    return None
