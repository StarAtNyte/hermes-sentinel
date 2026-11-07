"""Teaching examples for a rule, read from disk through a small byte-capped cache."""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ...vision import Example

logger = logging.getLogger(__name__)

__all__ = ["ExampleCache"]

EXAMPLE_CACHE_SIZE = 32
EXAMPLE_CACHE_BYTES = 16 * 1024 * 1024  # examples are small now; cap the cache by bytes too
MAX_EXAMPLE_FILE_BYTES = 4 * 1024 * 1024


class ExampleCache:
    """Loads a rule's positive/negative examples; file contents are cached by (path, mtime)."""

    def __init__(self, store: Any) -> None:
        """``store`` supplies ``list_examples(rule_id, kind, limit=...)``."""
        self._store = store
        self.entries: OrderedDict[tuple[str, float], bytes] = OrderedDict()
        self._lock = threading.Lock()  # guards entries (LRU order and byte total)

    def load(self, rule_id: str) -> list[Example]:
        """Up to two positive and two negative examples; empty when the store fails."""
        out: list[Example] = []
        try:
            records = [
                *self._store.list_examples(rule_id, "pos", limit=2),
                *self._store.list_examples(rule_id, "neg", limit=2),
            ]
        except Exception as exc:  # noqa: BLE001 - examples are optional teaching context
            logger.debug("sentinel: examples unavailable for %s: %s", rule_id, exc)
            return out
        for rec in records:
            data = self.read(rec.path)
            if data:
                out.append(Example(kind=rec.kind, data=data, note=rec.note or ""))
        return out

    def read(self, path: str) -> bytes | None:
        """The file's bytes (cached), or ``None`` when missing, empty or over the size cap."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_size <= 0 or st.st_size > MAX_EXAMPLE_FILE_BYTES:
            return None
        key = (path, st.st_mtime)
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        self._store_entry(key, data)
        return data

    def _cached(self, key: tuple[str, float]) -> bytes | None:
        with self._lock:
            data = self.entries.get(key)
            if data is not None:
                self.entries.move_to_end(key)
            return data

    def _store_entry(self, key: tuple[str, float], data: bytes) -> None:
        with self._lock:
            self.entries[key] = data
            total = sum(len(v) for v in self.entries.values())
            while self.entries and (
                len(self.entries) > EXAMPLE_CACHE_SIZE or total > EXAMPLE_CACHE_BYTES
            ):
                _, dropped = self.entries.popitem(last=False)
                total -= len(dropped)
