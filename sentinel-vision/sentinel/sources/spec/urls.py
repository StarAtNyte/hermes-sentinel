"""URL handling for source specs: credential redaction, secret placeholders, validation."""

from __future__ import annotations

import re
import urllib.parse

from ..constants import HTTP_SCHEMES, STREAM_PATH_HINTS
from ..errors import SourceSpecError
from .secrets import require_secret

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "token",
        "access_token",
        "key",
        "apikey",
        "api_key",
        "auth",
        "secret",
        "sig",
        "signature",
    }
)


def is_placeholder(value: str) -> bool:
    """Whether ``value`` is exactly one ``${NAME}`` secret placeholder."""
    return bool(value) and PLACEHOLDER_RE.fullmatch(value) is not None


def _redact_netloc(netloc: str) -> str:
    if "@" not in netloc:
        return netloc
    userinfo, _, hostport = netloc.rpartition("@")
    user, sep, password = userinfo.partition(":")
    if sep and not is_placeholder(password):
        password = "***"
    return f"{user}{sep}{password}@{hostport}"


def _redact_query(query: str) -> str:
    if not query:
        return query
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    redacted = [
        (k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS and not is_placeholder(v) else v)
        for k, v in pairs
    ]
    return urllib.parse.urlencode(redacted, safe="${}")


def redact_url(url: str) -> str:
    """Return ``url`` with any literal password and sensitive query values replaced by ``***``.

    ``${NAME}`` placeholders are kept (they name a secret, they are not one).
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid url>"
    netloc = _redact_netloc(parts.netloc)
    query = _redact_query(parts.query)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


NETWORK_KINDS = frozenset({"snapshot", "stream"})
CONFIG_ONLY_HINT = (
    "network sources (rtsp://, http(s) streams and snapshots) cannot be given directly: add the "
    "URL under plugins.entries.sentinel-vision.settings.stream_urls in config.yaml and use "
    "stream:<name>"
)


def require_configured(kind: str, configured: bool) -> None:
    """Refuse a network source that did not come from the user's own configuration.

    A model-supplied URL could point at a loopback or cloud-metadata address or carry a
    ``${NAME}`` placeholder that would send a Hermes secret to a host the model chose.
    """
    if kind in NETWORK_KINDS and not configured:
        raise SourceSpecError(CONFIG_ONLY_HINT)


def expand_url(
    template: str,
    *,
    origin: str,
    schemes: frozenset[str] | None = None,
    configured: bool = True,
) -> str:
    """Substitute ``${NAME}`` placeholders with URL-quoted secret values.

    A template that is exactly one placeholder (``${SENTINEL_STREAM_URL_1}``) holds a whole URL
    in the secret, so the value is used as is and, when ``schemes`` is given, checked against it.
    Placeholders are only expanded for a ``configured`` source (the user's own config).
    """
    if not configured and PLACEHOLDER_RE.search(template):
        raise SourceSpecError(
            "secret placeholders (${NAME}) are only allowed in configured sources (stream_urls)"
        )
    if is_placeholder(template.strip()):
        url = require_secret(PLACEHOLDER_RE.fullmatch(template.strip()).group(1), origin=origin)
        if schemes is not None:
            _split_template(url, schemes)
        return url

    def sub(match: re.Match[str]) -> str:
        return urllib.parse.quote(require_secret(match.group(1), origin=origin), safe="")

    return PLACEHOLDER_RE.sub(sub, template)


def _split_template(url: str, schemes: frozenset[str]) -> urllib.parse.SplitResult:
    """Parse ``url`` and check shape, scheme and host."""
    if not isinstance(url, str) or not url.strip():
        raise SourceSpecError("URL must be a non-empty string")
    if any(ch in url for ch in "\r\n\t\x00"):
        raise SourceSpecError("URL contains control characters")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SourceSpecError(f"invalid URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        allowed = ", ".join(sorted(schemes))
        raise SourceSpecError(
            f"unsupported URL scheme {scheme or '(none)'!r}; use one of {allowed}"
        )
    if not parts.hostname and not PLACEHOLDER_RE.search(parts.netloc):
        raise SourceSpecError("URL has no host")
    return parts


def _reject_literal_credentials(parts: urllib.parse.SplitResult) -> None:
    if "@" in parts.netloc:
        userinfo = parts.netloc.rpartition("@")[0]
        _, sep, password = userinfo.partition(":")
        if sep and password and not is_placeholder(password):
            raise SourceSpecError(
                "URL contains a literal password; reference a secret instead, e.g. "
                "rtsp://user:${CAMERA_PASSWORD}@host/path"
            )
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS and value and not is_placeholder(value):
            raise SourceSpecError(
                f"URL query parameter {key!r} looks like a credential; use ${{SECRET_NAME}} instead"
            )


def check_url_template(url: str, *, schemes: frozenset[str]) -> None:
    """Validate scheme and reject literal credentials in a URL template.

    A template that is a single ``${NAME}`` placeholder is accepted as is: the secret holds the
    whole URL and is checked when it is resolved.
    """
    if is_placeholder(url.strip()) and not any(ch in url for ch in "\r\n\t\x00"):
        return
    _reject_literal_credentials(_split_template(url, schemes))


def looks_like_stream(url: str) -> bool:
    """Whether ``url`` points at a video stream rather than a single-image endpoint."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in HTTP_SCHEMES:
        return True
    path = parts.path.lower()
    return path.endswith(STREAM_PATH_HINTS) or "mjpg" in path or "mjpeg" in path
