"""Grouping key for a rule's source."""

from __future__ import annotations

from ...sources import SourceSpec

__all__ = ["default_source_key"]


def default_source_key(spec: str, named_streams: dict[str, str] | None = None) -> str:
    """Grouping key for a rule's source: the canonical spec, or the raw text if unparsable."""
    try:
        return SourceSpec.parse(spec, named_streams=named_streams).canonical()
    except Exception:  # noqa: BLE001 - any unparsable spec still gets a stable key
        return str(spec).strip()
