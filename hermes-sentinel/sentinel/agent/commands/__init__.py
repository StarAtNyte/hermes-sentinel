"""The ``/sentinel`` slash command, one module per subcommand group."""

from .command import SentinelCommand
from .common import ARGS_HINT, USAGE

__all__ = ["ARGS_HINT", "USAGE", "SentinelCommand"]
