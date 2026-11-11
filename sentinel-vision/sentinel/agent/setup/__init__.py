"""One-command setup check: what is missing before Sentinel can watch and alert?

``run_checks(env)`` is pure and testable: every input (importability, camera probe, disk,
storage, engine, route book) is a field of :class:`SetupEnv`, so tests pass fakes. Surfaces:
``/sentinel setup``, the ``sentinel_setup_check`` tool and ``GET /setup`` on the dashboard API.
"""

from .env import SetupEnv
from .model import FAIL, OK, WARN, Check
from .report import fail_titles, format_report, summarize
from .runner import run_checks

__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "SetupEnv",
    "fail_titles",
    "format_report",
    "run_checks",
    "summarize",
]
