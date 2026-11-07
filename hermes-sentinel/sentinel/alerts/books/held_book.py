"""HeldAlerts: alerts that no route accepted, kept per rule until the user picks them up."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .json_file import read_json_object, write_json_atomic

__all__ = ["HeldAlert", "HeldAlerts", "describe_held"]


@dataclass(frozen=True)
class HeldAlert:
    """The latest undelivered alert of one rule and how many were held in total."""

    event_id: int
    count: int = 1


class HeldAlerts:
    """Thread-safe map ``rule_id -> HeldAlert``, persisted as JSON at ``path`` when given."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Create the book; ``path`` makes held alerts survive a restart."""
        self._path = Path(path) if path is not None else None
        # Guards _held and the held-alerts file write.
        self._lock = threading.Lock()
        self._held: dict[str, HeldAlert] = {}
        for rule_id, raw in read_json_object(self._path, "held alerts").items():
            held = _parse(raw)
            if held is not None:
                self._held[rule_id] = held

    def hold(self, rule_id: str, event_id: int) -> HeldAlert:
        """Record that the alert for ``event_id`` of ``rule_id`` could not be delivered."""
        with self._lock:
            previous = self._held.get(rule_id)
            held = HeldAlert(event_id, (previous.count if previous else 0) + 1)
            self._held[rule_id] = held
            self._save_locked()
            return held

    def get(self, rule_id: str) -> HeldAlert | None:
        """The held alert of ``rule_id``, if any."""
        with self._lock:
            return self._held.get(rule_id)

    def clear(self, rule_id: str) -> None:
        """Forget the held alert of ``rule_id`` (it was delivered or shown)."""
        with self._lock:
            if self._held.pop(rule_id, None) is not None:
                self._save_locked()

    def _save_locked(self) -> None:
        write_json_atomic(self._path, {k: asdict(v) for k, v in self._held.items()}, "held alerts")


def _parse(raw: object) -> HeldAlert | None:
    if not isinstance(raw, dict):
        return None
    event_id, count = raw.get("event_id"), raw.get("count", 1)
    if isinstance(event_id, int) and isinstance(count, int) and count >= 1:
        return HeldAlert(event_id, count)
    return None


def describe_held(rule_id: str, held: HeldAlert) -> str:
    """One status line telling the user how to receive the held alert of ``rule_id``."""
    more = f" ({held.count} held, latest event #{held.event_id})" if held.count > 1 else ""
    return f"alert held for rule {rule_id}{more}: run /sentinel resume {rule_id} here to receive it"
