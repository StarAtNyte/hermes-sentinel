"""Grouping key for a rule's source."""

from __future__ import annotations

import logging

from ...sources import SourceError, SourceSpec

logger = logging.getLogger(__name__)

__all__ = ["default_source_key"]


def default_source_key(spec: str, named_streams: dict[str, str] | None = None) -> str:
    """Grouping key for a rule's source: the canonical spec, or the raw text if unparsable."""
    try:
        return SourceSpec.parse(spec, named_streams=named_streams).canonical()
    except (SourceError, ValueError, TypeError) as exc:
        logger.debug("sentinel: unparsable source spec keyed as text: %s", type(exc).__name__)
        return str(spec).strip()
