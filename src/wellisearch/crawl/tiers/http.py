"""T0 Http tier: curl_cffi with Chrome impersonation (design §3.1).

Cheapest tier: a single TLS-fingerprinted request, no browser. Good for the
majority of sites that don't run a JS challenge.
"""
from __future__ import annotations

import logging
import re
import time

from ...config import get_settings
from ..policy import Policy
from ..results import Rendered
from . import register

log = logging.getLogger("wellisearch.crawl.tiers.http")


class HttpTier:
    """curl_cffi AsyncSession with Chrome impersonation."""

    name = "http"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """One impersonated GET; returns html + status + title."""
        from curl_cffi.requests import AsyncSession

        s = get_settings()
        start = time.monotonic()
        async with AsyncSession(impersonate="chrome") as sess:
            r = await sess.get(url, timeout=s.CRAWL_TIMEOUT_S)
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(
            html=r.text,
            title=_extract_title(r.text),
            status=r.status_code,
            ms=ms,
            engine="http",
        )

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget: a single impersonated GET (CRAWL_TIMEOUT_S)."""
        return float(get_settings().CRAWL_TIMEOUT_S)


def _extract_title(html: str) -> str | None:
    """Best-effort <title> via a regex; None when absent."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
        if t:
            return t
    return None


register(HttpTier())
