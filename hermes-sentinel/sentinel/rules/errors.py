"""Errors raised by the rules package."""

from __future__ import annotations


class RuleValidationError(ValueError):
    """A rule spec (or one of its fields) is malformed."""
