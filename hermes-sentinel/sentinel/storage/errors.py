"""Storage exceptions."""

from __future__ import annotations


class StoreError(RuntimeError):
    """The store could not be opened or an operation failed irrecoverably."""
