"""Rule compilation: plain English to a validated rule."""

from __future__ import annotations

from .compiler import compile_rule
from .fallback import fallback_rule
from .schema import COMPILE_INSTRUCTIONS, COMPILE_SCHEMA

__all__ = ["COMPILE_INSTRUCTIONS", "COMPILE_SCHEMA", "compile_rule", "fallback_rule"]
