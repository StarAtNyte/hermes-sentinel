"""Cheap keyword heuristic: does a rule's question ask about movement over time?"""

from __future__ import annotations

import re

__all__ = ["looks_like_motion"]

_MOTION_WORDS = (
    r"moving|moves?|moved|motion|walking|walks?|running|runs?|approach(?:es|ing)?|"
    r"leaving|leaves|entering|enters|falling|falls?|fell|pouring|pours?|spilling|spills?|"
    r"opening|closing|swinging|waving|tumbling|rolling|climbing|heading|toward|towards|"
    r"away from|getting (?:closer|bigger|smaller|higher|lower)|rising|sinking|dripping"
)
_MOTION_RE = re.compile(rf"\b(?:{_MOTION_WORDS})\b", re.IGNORECASE)


def looks_like_motion(*texts: str) -> bool:
    """True when any text names a movement or change over time (default for stills: False).

    Deliberately conservative: a missed motion rule still works as a still-image rule, while a
    false positive only costs a few more (downscaled) images in the same single call.
    """
    return any(_MOTION_RE.search(text) for text in texts if isinstance(text, str))
