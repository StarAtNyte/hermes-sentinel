"""Registration of the plugin's two auxiliary LLM tasks."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["register_aux_tasks"]


def register_aux_tasks(ctx: Any) -> list[str]:
    """Register the plugin's two LLM tasks (vision judge, rule compiler). Never raises."""
    from ..rules import RULES_TASK, RULES_TASK_INFO
    from ..vision import VISION_TASK, VISION_TASK_INFO

    done = []
    for key, info in ((VISION_TASK, VISION_TASK_INFO), (RULES_TASK, RULES_TASK_INFO)):
        try:
            ctx.register_auxiliary_task(key, **info)
            done.append(key)
        except Exception as exc:  # noqa: BLE001 - a host without aux tasks still gets tools
            logger.warning("sentinel: could not register auxiliary task %s: %s", key, exc)
    return done
