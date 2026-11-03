"""Short-URL resolution: follow known shorteners (e.g. Amazon's a.co) to their final URL before crawling.

The engine selects policy + extractor by hostname, so an unresolved a.co link
would take the generic path even when it points at an amazon.com product page.
resolve_short_url() follows the redirect chain with an impersonated HEAD,
falling back to GET headers when HEAD is refused or does not redirect. Neither
request downloads the final body. The cookie jar carries cookies across hops.
Short URLs are never stored or indexed: callers that get None (unresolvable)
must fail the crawl instead of falling back to the short form.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..config import get_settings
from .probe import clamp

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession, Response

log = logging.getLogger("wellisearch.crawl.shorturl")

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_WRITE_ERROR = 0xFFFFFFFF  # libcurl's explicit write/header abort sentinel.


def is_short_url(url: str) -> bool:
    """True when the URL's host is a configured shortener (e.g. a.co)."""
    host = (urlparse(url).hostname or "").lower()
    return host in get_settings().short_url_hosts


async def resolve_short_url(url: str) -> str | None:
    """Follow a short URL to its final destination; None when it cannot be resolved.

    Follow redirects with HEAD, then GET headers if needed. Abort the final
    header callback, before libcurl can download or enqueue the response body.
    Both attempts share a wall-clock deadline and a cookie jar, and each has a
    finite redirect limit. Multi-hop chains are handled by curl_cffi itself.
    Returns the original url unchanged for non-short URLs. None is returned on
    any failure — network error, timeout, or a chain that never leaves the
    shortener host (e.g. a JS/meta redirect the HTTP client cannot follow)."""
    if not is_short_url(url):
        return url
    s = get_settings()
    try:
        from curl_cffi.requests import AsyncSession

        async with asyncio.timeout(clamp(s.CRAWL_TIMEOUT_S)):
            async with AsyncSession(impersonate="chrome", max_clients=1) as sess:
                final = await _resolve_headers(sess, url)
    except Exception as e:
        log.warning("short-url resolve failed for %s: %s", url, e)
        return None
    if final is None:
        # The chain never left the shortener (JS/meta redirect or a loop): we
        # cannot name the real page, so refuse rather than index the short form.
        log.warning("short url %s did not resolve off its shortener host", url)
        return None
    if final != url:
        log.info("resolved short url %s -> %s", url, final)
    return final


# ---------------------------------------------------------------------------
# Header-Only Redirects
# ---------------------------------------------------------------------------

async def _resolve_headers(sess: AsyncSession, url: str) -> str | None:
    """Try HEAD first; a refused/misimplemented HEAD must not hide a valid GET redirect."""
    if callable(getattr(sess, "head", None)):
        try:
            r = await _request_headers(sess, url, "head")
            final = _destination(r)
            if final is not None and r.status_code < 400:
                return final
        except Exception:
            # Some shorteners close HEAD connections instead of returning 405.
            # Cancellation/deadline still propagates (CancelledError is not Exception).
            pass
    return _destination(await _request_headers(sess, url, "get"))


async def _request_headers(
    sess: AsyncSession,
    url: str,
    method: str,
) -> Response:
    """Read redirects and final headers, translating only our deliberate write abort."""
    from curl_cffi import CurlECode, CurlOpt
    from curl_cffi.requests.exceptions import RequestException

    s = get_settings()
    headers = _RedirectHeaders()
    sess.curl_options = {
        CurlOpt.HEADERFUNCTION: headers.header,
        CurlOpt.SUPPRESS_CONNECT_HEADERS: 1,
    }
    try:
        return await getattr(sess, method)(
            url,
            allow_redirects=True,
            content_callback=headers.discard,
            max_redirects=s.CRAWL_HTTP_MAX_REDIRECTS,
            timeout=clamp(s.CRAWL_TIMEOUT_S),
            verify=not s.CRAWL_IGNORE_SSL_ERRORS,
        )
    except RequestException as e:
        if headers.finished and e.code == CurlECode.WRITE_ERROR and e.response is not None:
            return e.response
        raise


def _destination(r: Response) -> str | None:
    """Accept only a named HTTP(S) destination outside the configured shorteners."""
    final = str(r.url)
    parsed = urlparse(final)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or is_short_url(final):
        return None
    return final


class _RedirectHeaders:
    """Stop at the final header block rather than waiting for its first body chunk."""

    def __init__(self) -> None:
        """Initialize redirect-header state without retaining any header/body data."""
        self.finished = False
        self._has_location = False
        self._status = 0

    def header(self, chunk: bytes) -> int:
        """Allow informational/redirect blocks and abort final response headers."""
        if chunk.startswith(b"HTTP/"):
            self._status = int(chunk.split(maxsplit=2)[1])
            self._has_location = False
        elif chunk.lower().startswith(b"location:"):
            self._has_location = bool(chunk.partition(b":")[2].strip())
        elif not chunk.strip() and self._status >= 200:
            if self._status not in _REDIRECT_STATUSES or not self._has_location:
                self.finished = True
                return _WRITE_ERROR
        return len(chunk)

    def discard(self, chunk: bytes) -> int:
        """Never buffer a body, even if an unusual server bypasses final headers."""
        return _WRITE_ERROR
