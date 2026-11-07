"""Recording a fired rule: snapshot, history event and the alert."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...storage import EventRecord, to_iso
from ...vision import Verdict, encode_jpeg
from ..models import Job, RuleRuntime
from ..state import EngineState

logger = logging.getLogger(__name__)

__all__ = ["EventRecorder"]


class EventRecorder:
    """Stores the snapshot and event of a rule that fired and sends the alert."""

    def __init__(self, state: EngineState) -> None:
        """Share the engine ``state`` (store, alerter, clocks, config)."""
        self._state = state

    def fire(self, rt: RuleRuntime, job: Job, verdict: Verdict) -> None:
        """Save the snapshot and event for ``verdict`` and alert; a store failure still alerts."""
        state = self._state
        spec = rt.spec
        wall = state.wall()
        snapshot = self._save_snapshot(job.frame, wall, spec.id)
        caption = verdict.caption or f"Rule matched: {spec.question}"
        event = self._store_event(rt, verdict, caption, snapshot, wall)
        rt.last_event_id = event.id
        state.fired += 1
        state.alerter.alert_event(
            spec, event, source_name=rt.source_key, include_media=state.config.alert_snapshots
        )

    def _save_snapshot(self, frame: Any, wall: float, rule_id: str) -> Path | str | None:
        jpeg = encode_jpeg(frame)
        if jpeg is None:
            return None
        try:
            return self._state.store.save_snapshot(jpeg, ts=wall, rule_id=rule_id)
        except Exception as exc:  # noqa: BLE001 - the alert goes out without a picture
            logger.warning("sentinel: could not save snapshot: %s", exc)
            return None

    def _store_event(
        self, rt: RuleRuntime, verdict: Verdict, caption: str, snapshot: Any, wall: float
    ) -> EventRecord:
        spec = rt.spec
        try:
            return self._state.store.add_event(
                spec.id,
                caption=caption,
                reason=verdict.reason,
                confidence=verdict.confidence,
                source=rt.source_key,
                snapshot_path=snapshot,
                ts=wall,
            )
        except Exception as exc:  # noqa: BLE001 - still alert even if the history write fails
            logger.warning("sentinel: could not store event: %s", exc)
            return EventRecord(
                id=0,
                rule_id=spec.id,
                ts=to_iso(wall),
                caption=caption,
                reason=verdict.reason,
                confidence=verdict.confidence,
                source=rt.source_key,
                snapshot_path=str(snapshot) if snapshot else None,
                feedback=None,
            )
