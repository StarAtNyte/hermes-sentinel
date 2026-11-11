"""Setup checks about cameras, screens, streams and their secrets."""

from .default_source import check_default_source
from .secrets import check_secrets
from .sources import check_sources

__all__ = ["check_default_source", "check_secrets", "check_sources"]
