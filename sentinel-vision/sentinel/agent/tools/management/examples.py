"""Turn an event's photo into a teaching example for its rule."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ....storage import EventRecord
from ..common import ToolContext, small_jpeg

__all__ = ["example_from_event"]


def example_from_event(
    context: ToolContext, rule_id: str, kind: str, event: EventRecord, note: str
) -> Any:
    """Add an event's photo as an example, shrunk to the example size when possible."""
    store = context.sentinel.store
    data = None
    try:
        if event.snapshot_path:
            data = Path(event.snapshot_path).read_bytes()
    except OSError:
        data = None
    if data:
        return store.add_example(rule_id, kind, small_jpeg(data), note=note)
    return store.add_example(rule_id, kind, event_id=event.id, note=note)
