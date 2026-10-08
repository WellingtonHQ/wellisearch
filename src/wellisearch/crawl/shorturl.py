"""Short-URL resolution: follow known shorteners (e.g. Amazon's a.co) to their final URL before crawling.

The engine selects policy + extractor by hostname, so an unresolved a.co link
would take the generic path even when it points at an amazon.com product page.
resolve_short_url() follows the redirect chain with one impersonated GET
(curl_cffi's cookie jar carries session cookies across hops) and returns the
final URL so downstream selection sees the real site. Short URLs are never
stored or indexed: callers that get None (unresolvable) must fail the crawl
instead of falling back to the short form.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..config import get_settings
from .probe import clamp

log = logging.getLogger("wellisearch.crawl.shorturl")


def is_short_url(url: str) -> bool:
    """True when the URL's host is a configured shortener (e.g. a.co)."""
    host = (urlparse(url).hostname or "").lower()
    return host in get_settings().short_url_hosts


async def resolve_short_url(url: str) -> str | None:
    """Follow a short URL to its final destination; None when it cannot be resolved.

    One impersonated GET with redirect-following enabled, so multi-hop chains
    (and cookie-bound intermediate hops) are handled by curl_cffi itself.
    Returns the original url unchanged for non-short URLs. None is returned on
    any failure — network error, timeout, or a chain that never leaves the
    shortener host (e.g. a JS/meta redirect the HTTP client cannot follow)."""
    if not is_short_url(url):
        return url
    s = get_settings()
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome") as sess:
            r = await sess.get(
                url, timeout=clamp(s.CRAWL_TIMEOUT_S), verify=not s.CRAWL_IGNORE_SSL_ERRORS
            )
    except Exception as e:
        log.warning("short-url resolve failed for %s: %s", url, e)
        return None
    final = str(r.url)
    if is_short_url(final):
        # The chain never left the shortener (JS/meta redirect or a loop): we
        # cannot name the real page, so refuse rather than index the short form.
        log.warning("short url %s did not resolve off its shortener host", url)
        return None
    if final != url:
        log.info("resolved short url %s -> %s", url, final)
    return final
