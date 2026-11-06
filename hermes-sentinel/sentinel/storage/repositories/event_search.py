"""Pure SQL builders for event filtering and free-text search."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..records import as_iso

EVENT_COLS = (
    "e.id, e.rule_id, e.ts, e.caption, e.reason, e.confidence, e.source, "
    "e.snapshot_path, e.feedback"
)
TERM_RE = re.compile(r"[\w]+", re.UNICODE)
STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "anything", "are", "at", "be", "did", "do", "does", "for",
        "from", "happened", "has", "have", "i", "in", "is", "it", "me", "my", "of", "on",
        "or", "the", "to", "today", "was", "were", "what", "when", "where", "which", "who",
        "with", "yesterday",
    }
)  # fmt: skip


def search_terms(text: str) -> list[str]:
    """Meaningful lower-case words of ``text`` (at most 16, deduplicated, in order).

    Stopword-only queries ("what happened?") give no terms: the caller returns nothing
    rather than every event containing "the".
    """
    words = [w.lower() for w in TERM_RE.findall(text or "")]
    terms = [w for w in words if w not in STOPWORDS and len(w) > 1]
    return list(dict.fromkeys(terms))[:16]


def filter_clauses(
    *,
    rule_id: str | None,
    since: datetime | float | str | None,
    until: datetime | float | str | None,
    feedback: str | None,
) -> tuple[list[str], list[Any]]:
    """``WHERE`` fragments and parameters for the structured filters."""
    where, params = ["1=1"], []
    if rule_id is not None:
        where.append("e.rule_id = ?")
        params.append(rule_id)
    if since is not None:
        where.append("e.ts >= ?")
        params.append(as_iso(since))
    if until is not None:
        where.append("e.ts < ?")
        params.append(as_iso(until))
    if feedback == "none":
        where.append("e.feedback IS NULL")
    elif feedback is not None:
        where.append("e.feedback = ?")
        params.append(feedback)
    return where, params


def fts_query(
    terms: list[str], where: list[str], params: list[Any], limit: int
) -> tuple[str, list[Any]]:
    """Relevance-ranked FTS5 prefix query over ``terms``."""
    match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in terms)
    sql = (
        f"SELECT {EVENT_COLS} FROM events_fts f JOIN events e ON e.id = f.rowid "
        f"WHERE events_fts MATCH ? AND {' AND '.join(where)} "
        "ORDER BY bm25(events_fts), e.ts DESC LIMIT ?"
    )
    return sql, [match, *params, limit]


def like_query(
    terms: list[str], where: list[str], params: list[Any], limit: int
) -> tuple[str, list[Any]]:
    """Newest-first ``LIKE`` query; with no ``terms`` only the structured filters apply."""
    where, params = list(where), list(params)
    if terms:
        like = []
        for t in terms:
            pattern = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern += "%"
            like.append("(e.caption LIKE ? ESCAPE '\\' OR e.reason LIKE ? ESCAPE '\\')")
            params.extend([pattern, pattern])
        where.append("(" + " OR ".join(like) + ")")
    sql = (
        f"SELECT {EVENT_COLS} FROM events e WHERE {' AND '.join(where)} "
        "ORDER BY e.ts DESC, e.id DESC LIMIT ?"
    )
    return sql, [*params, limit]
