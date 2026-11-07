"""``GET /setup``: the readiness checklist."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..common import access
from ..common.http import reply, run_blocking
from ..common.masking import mask

router = APIRouter()

__all__ = ["router"]


def _setup(probe: bool) -> dict[str, Any]:
    live = access.need()
    mod = access.module("agent.setup")
    checks = mod.run_checks(mod.SetupEnv.from_sentinel(live, None, probe_sources=probe))
    out: dict[str, Any] = {
        "ok": True,
        "probed": probe,
        **mod.summarize(checks),
        "summary": mod.format_report(checks).splitlines()[-1],
    }
    out["checks"] = [{**c.to_dict(), "detail": mask(c.detail), "fix": mask(c.fix)} for c in checks]
    return out


@router.get("/setup")
async def setup(probe: str | None = None) -> Any:
    """Setup checklist. Devices are only probed with ``?probe=1`` (probing opens webcams)."""
    result = await run_blocking(_setup, (probe or "").lower() in ("1", "true", "yes"))
    return reply(result)
