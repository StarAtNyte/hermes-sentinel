"""Source specs: the validated value object, its parser and URL/secret helpers."""

from .model import SourceSpec
from .parser import named_stream, parse_spec
from .urls import expand_url, redact_url, require_secret

__all__ = [
    "SourceSpec",
    "expand_url",
    "named_stream",
    "parse_spec",
    "redact_url",
    "require_secret",
]
