"""HTTP(S) snapshot source: one JPEG/PNG per request."""

from __future__ import annotations

import base64
import contextlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .. import dependencies
from ..base import BaseSource, Frame
from ..constants import HTTP_SCHEMES, MAX_SNAPSHOT_BYTES
from ..errors import SourceDisconnected, SourceError, SourceSpecError
from ..frames import decode_image
from ..spec import SourceSpec, expand_url, require_secret

_USER_AGENT = "hermes-sentinel/0.1.0 (+https://github.com/NousResearch/hermes-agent)"


class SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects but drop the Authorization header when the host changes."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Return the redirected request, stripping credentials on a cross-host hop."""
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_parts = urllib.parse.urlsplit(new.full_url)
        if new_parts.scheme.lower() not in HTTP_SCHEMES:
            return None
        if new_parts.netloc.lower() != old_host:
            for key in list(new.headers):
                if key.lower() == "authorization":
                    del new.headers[key]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


class SnapshotUrlSource(BaseSource):
    """HTTP(S) endpoint that returns one JPEG/PNG per request (polled on every ``read``).

    Uses urllib with a timeout and a response size cap. Credentials come only from the
    secret scope: ``${NAME}`` placeholders in the URL, basic auth via ``username_secret`` /
    ``password_secret``, or a bearer token via ``token_secret``.
    """

    kind = "snapshot"

    def __init__(
        self,
        spec: SourceSpec,
        *,
        clock: Callable[[], float] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        """Require a snapshot spec; ``opener`` and ``max_bytes`` are injectable for tests."""
        if spec.kind != "snapshot":
            raise SourceSpecError(f"SnapshotUrlSource needs a snapshot spec, got {spec.kind!r}")
        super().__init__(spec, clock=clock)
        self._opener = opener or urllib.request.build_opener(SameHostRedirectHandler())
        self._max_bytes = int(max_bytes)
        self._url: str | None = None
        self._headers: dict[str, str] = {}

    def __repr__(self) -> str:  # never show the resolved URL or headers
        """Show kind and state only."""
        return super().__repr__()

    def _auth_headers(self) -> dict[str, str]:
        spec = self.spec
        if spec.token_secret:
            token = require_secret(spec.token_secret, origin=self.name)
            return {"Authorization": f"Bearer {token}"}
        if spec.username_secret and spec.password_secret:
            user = require_secret(spec.username_secret, origin=self.name)
            password = require_secret(spec.password_secret, origin=self.name)
            raw = f"{user}:{password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}

    def open(self) -> None:
        """Resolve the URL and credentials; no network traffic happens until ``read``."""
        with self._lock:
            if self._is_open:
                return
            # fail early with an install hint
            dependencies.require_cv2(f"snapshot source {self.name!r}")
            self._url = expand_url(str(self.spec.target), origin=self.name)
            self._headers = {"User-Agent": _USER_AGENT, "Accept": "image/*", **self._auth_headers()}
            self._is_open = True

    def _check_response(self, resp: Any) -> None:
        status = getattr(resp, "status", 200)
        if status is not None and not 200 <= int(status) < 300:
            raise SourceDisconnected(f"{self.name}: HTTP {status}")
        length = resp.headers.get("Content-Length") if resp.headers else None
        if length and length.isdigit() and int(length) > self._max_bytes:
            raise SourceError(f"{self.name}: image larger than {self._max_bytes} bytes")

    def _http_error(self, exc: urllib.error.HTTPError) -> SourceError:
        code = exc.code
        with contextlib.suppress(Exception):
            exc.close()
        if code in (401, 403):
            return SourceError(f"{self.name}: HTTP {code} (check the configured secrets)")
        return SourceDisconnected(f"{self.name}: HTTP {code}")

    def _fetch(self) -> bytes:
        assert self._url is not None
        request = urllib.request.Request(self._url, headers=self._headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.spec.timeout) as resp:
                self._check_response(resp)
                data = resp.read(self._max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise SourceDisconnected(f"{self.name}: unreachable ({exc.reason})") from None
        except TimeoutError:
            raise SourceDisconnected(
                f"{self.name}: timed out after {self.spec.timeout:.0f}s"
            ) from None
        except OSError as exc:
            raise SourceDisconnected(f"{self.name}: network error ({exc})") from None
        if len(data) > self._max_bytes:
            raise SourceError(f"{self.name}: image larger than {self._max_bytes} bytes")
        return data

    def read(self) -> tuple[float, Frame]:
        """Fetch and decode one image."""
        with self._lock:
            self._require_open()
            data = self._fetch()
            ts = self.clock()
            return ts, decode_image(data, origin=self.name)

    def close(self) -> None:
        """Forget the resolved URL and headers."""
        with self._lock:
            self._is_open = False
            self._url = None
            self._headers = {}
