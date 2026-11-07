"""``GET /frame/{source}`` and ``GET /events/{id}/snapshot``: live frames and event photos."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from ..common import access
from ..common.http import NO_STORE, ApiError, check_event_id, fail, run_blocking
from ..common.masking import mask

router = APIRouter()

__all__ = ["router"]

MAX_KEY = 512
FRAME_MAX_SIDE = 640
FRAME_QUALITY = 75
MAX_DATA_URL_BYTES = 4 * 1024 * 1024  # larger images are not inlined


def _frame(source_key: str) -> bytes:
    if not source_key or len(source_key) > MAX_KEY:
        raise ApiError(400, "invalid source key")
    live = access.need()
    engine = live.engine_if_built
    if engine is None or not engine.running:
        raise ApiError(404, "no frame available: the watcher is not running")
    # Clients may percent-encode the key (it contains %2A after redaction), and proxies may
    # decode once more: compare the decoded forms.
    internal = {unquote(mask(k)): k for k in engine.source_infos()}
    key = internal.get(unquote(source_key))
    frame = engine.latest_frame(key) if key is not None else None
    if frame is None:
        raise ApiError(404, "no frame available for this source yet")
    encode_jpeg = access.module("vision.imaging").encode_jpeg
    data = encode_jpeg(frame, max_side=FRAME_MAX_SIDE, quality=FRAME_QUALITY)
    if data is None:
        raise ApiError(404, "could not encode the frame")
    return data


def _snapshot(event_id: str) -> bytes:
    eid = check_event_id(event_id)
    store = access.need().store
    event = store.get_event(eid)
    if event is None:
        raise ApiError(404, f"no event #{eid}")
    if not event.snapshot_path:
        raise ApiError(404, "this event has no snapshot")
    path = Path(event.snapshot_path).resolve()
    root = Path(store.data_dir).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ApiError(404, "the snapshot is gone")
    try:
        return path.read_bytes()
    except OSError:
        raise ApiError(404, "the snapshot is gone") from None


def _image_response(data: bytes, encoding: str | None, headers: dict[str, str]) -> Response:
    """Raw JPEG by default; ``encoding=data_url`` returns ``{ok, data_url}`` JSON instead.

    Hermes' Desktop plugin REST bridge only carries JSON, so the pane asks for the data URL.
    """
    if encoding is None or encoding == "":
        return Response(data, media_type="image/jpeg", headers=headers)
    if encoding != "data_url":
        return fail(400, "encoding must be 'data_url' when given")
    if len(data) > MAX_DATA_URL_BYTES:
        return fail(413, "image too large to inline")
    url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    return JSONResponse({"ok": True, "data_url": url}, headers=headers)


@router.get("/frame/{source_key:path}")
async def frame(source_key: str, encoding: str | None = None) -> Any:
    """The latest captured frame of a source as JPEG (or a data URL)."""
    result = await run_blocking(_frame, source_key)
    if isinstance(result, Response):
        return result
    return _image_response(result, encoding, NO_STORE)


@router.get("/events/{event_id}/snapshot")
async def snapshot(event_id: str, encoding: str | None = None) -> Any:
    """The saved photo of an event as JPEG (or a data URL)."""
    result = await run_blocking(_snapshot, event_id)
    if isinstance(result, Response):
        return result
    return _image_response(result, encoding, {"Cache-Control": "private, max-age=3600"})
