"""Source specs: the validated value object, its parser and URL/secret helpers."""

from .model import SourceSpec
from .parser import named_stream, parse_spec
from .secret_status import SecretStatus, probe_secrets, referenced_secrets
from .secrets import clear_cached_secrets, prime_secrets, require_secret
from .urls import PLACEHOLDER_RE, expand_url, redact_url, require_configured

__all__ = [
    "SecretStatus",
    "PLACEHOLDER_RE",
    "SourceSpec",
    "clear_cached_secrets",
    "expand_url",
    "require_configured",
    "named_stream",
    "parse_spec",
    "prime_secrets",
    "probe_secrets",
    "redact_url",
    "referenced_secrets",
    "require_secret",
]
