"""Table repositories; each owns the SQL for one concern and shares one Database."""

from __future__ import annotations

from .events import EventRepository
from .examples import ExampleRepository
from .rules import RuleRepository
from .usage import UsageRepository

__all__ = ["EventRepository", "ExampleRepository", "RuleRepository", "UsageRepository"]
