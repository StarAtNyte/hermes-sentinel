"""Field validation for :class:`SourceSpec`, split per concern."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..constants import (
    HTTP_SCHEMES,
    MAX_CAMERA_INDEX,
    MAX_DIMENSION,
    MAX_SCREEN_INDEX,
    SOURCE_KINDS,
    STREAM_SCHEMES,
)
from ..errors import SourceSpecError
from ..timeouts import clamp_timeout
from .urls import check_url_template

if TYPE_CHECKING:
    from .model import SourceSpec

_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_FIELDS = ("username_secret", "password_secret", "token_secret")


def _validate_index(spec: SourceSpec) -> None:
    if isinstance(spec.target, bool) or not isinstance(spec.target, int):
        raise SourceSpecError(f"{spec.kind} target must be an integer index")
    upper = MAX_CAMERA_INDEX if spec.kind == "camera" else MAX_SCREEN_INDEX
    if not 0 <= spec.target <= upper:
        raise SourceSpecError(f"{spec.kind} index must be between 0 and {upper}")


def _validate_stream(spec: SourceSpec) -> None:
    check_url_template(str(spec.target), schemes=STREAM_SCHEMES)


def _validate_snapshot(spec: SourceSpec) -> None:
    check_url_template(str(spec.target), schemes=HTTP_SCHEMES)


def _validate_folder(spec: SourceSpec) -> None:
    if not isinstance(spec.target, str) or not spec.target.strip():
        raise SourceSpecError("folder target must be a non-empty path")
    if "\x00" in spec.target:
        raise SourceSpecError("folder path contains a NUL byte")


_TARGET_VALIDATORS: dict[str, Callable[[SourceSpec], None]] = {
    "camera": _validate_index,
    "screen": _validate_index,
    "stream": _validate_stream,
    "snapshot": _validate_snapshot,
    "folder": _validate_folder,
}


def _validate_region(spec: SourceSpec) -> None:
    if spec.region is None:
        return
    if spec.kind != "screen":
        raise SourceSpecError("region is only valid for screen sources")
    if len(spec.region) != 4:
        raise SourceSpecError("region must be (left, top, width, height)")
    left, top, width, height = (int(v) for v in spec.region)
    if width <= 0 or height <= 0:
        raise SourceSpecError("region width and height must be positive")
    object.__setattr__(spec, "region", (left, top, width, height))


def _validate_dimensions(spec: SourceSpec) -> None:
    for dim in ("width", "height"):
        value = getattr(spec, dim)
        if value is not None and (not isinstance(value, int) or not 0 < value <= MAX_DIMENSION):
            raise SourceSpecError(f"{dim} must be a positive integer <= {MAX_DIMENSION}")


def _validate_secrets(spec: SourceSpec) -> None:
    for attr in _SECRET_FIELDS:
        value = getattr(spec, attr)
        if value is None:
            continue
        if spec.kind != "snapshot":
            raise SourceSpecError(f"{attr} is only valid for snapshot sources")
        if not _SECRET_NAME_RE.match(value):
            raise SourceSpecError(f"{attr} must be a secret name like CAM_PASSWORD")
    if (spec.username_secret is None) != (spec.password_secret is None):
        raise SourceSpecError("username_secret and password_secret must be given together")
    if spec.token_secret and spec.password_secret:
        raise SourceSpecError("use either basic auth secrets or token_secret, not both")


def validate_spec(spec: SourceSpec) -> None:
    """Validate every field of ``spec`` and normalise timeout and region in place.

    Raises:
        SourceSpecError: the spec is malformed or unsafe.
    """
    if spec.kind not in SOURCE_KINDS:
        raise SourceSpecError(f"unknown source kind {spec.kind!r}")
    object.__setattr__(spec, "timeout", clamp_timeout(spec.timeout))
    _TARGET_VALIDATORS[spec.kind](spec)
    _validate_region(spec)
    _validate_dimensions(spec)
    _validate_secrets(spec)
