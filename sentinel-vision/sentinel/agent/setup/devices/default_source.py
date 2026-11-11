"""Check that ``default_source`` parses and, for local devices, was found by the probe."""

from __future__ import annotations

from typing import Any

from ....sources import SourceSpec, SourceSpecError
from ..env import SetupEnv
from ..masking import mask, named_streams
from ..model import FAIL, OK, WARN, Check

__all__ = ["check_default_source"]


def check_default_source(env: SetupEnv, infos: list[Any]) -> Check:
    """FAIL for an unparsable spec, WARN when a camera/screen was not among the probed ones."""
    title = "Default source"
    raw = getattr(env.config, "default_source", "camera:0")
    try:
        spec = SourceSpec.parse(raw, named_streams=named_streams(env))
    except SourceSpecError as exc:
        return Check(
            "default_source",
            title,
            FAIL,
            f"default_source {mask(raw)!r} is not valid: {mask(exc)}",
            "Set default_source to camera:N, screen[:N], rtsp://..., stream:<name>, "
            "snapshot:https://... or folder:/path in the plugin settings.",
        )
    label = mask(spec.name or raw)
    if spec.kind in ("camera", "screen") and infos:
        want = f"{spec.kind}:{spec.target}"
        known = {i.spec for i in infos if i.available is True}
        if spec.kind == "screen" and spec.target == 0:
            known |= {"screen:0"}
        if known and want not in known:
            return Check(
                "default_source",
                title,
                WARN,
                f"{want} was not found; available: {', '.join(sorted(known))}.",
                "Change default_source to one of those.",
            )
    return Check("default_source", title, OK, f"{label} parses as a {spec.kind} source.")
