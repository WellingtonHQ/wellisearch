"""T2 Stealth tier: Scrapling StealthySession (design §3.1).

Scrapling's StealthySession is a SYNC API, so it runs in a worker thread via
asyncio.to_thread to avoid blocking the event loop. Last resort for hard CF
grids and metered paywalls.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ...config import get_settings
from ..policy import Policy
from ..results import Rendered
from . import register

log = logging.getLogger("wellisearch.crawl.tiers.stealth")


class StealthTier:
    """Scrapling StealthySession: CF auto-solve, paywalls (last resort)."""

    name = "stealth"

    async def fetch(
        self,
        url: str,
        p: Policy,
    ) -> Rendered:
        """Run the sync StealthySession in a worker thread."""
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> Rendered:
        from scrapling.fetchers import StealthySession

        s = get_settings()
        start = time.monotonic()
        with StealthySession(headless=s.CRAWL_HEADLESS) as session:
            page = session.fetch(url, network_idle=True, timeout=s.CRAWL_STEALTH_TIMEOUT_S * 1000)
        ms = int((time.monotonic() - start) * 1000)
        return Rendered(
            html=page.html_content,
            title=_extract_title(page),
            status=getattr(page, "status", 200),
            ms=ms,
            engine="stealth",
        )

    def worst_case_s(self, p: Policy) -> float:
        """Worst-case budget: a single StealthySession fetch (CRAWL_STEALTH_TIMEOUT_S)."""
        return float(get_settings().CRAWL_STEALTH_TIMEOUT_S)


def _extract_title(page: object) -> str | None:
    """Best-effort <title> from the Scrapling Response; None on any failure."""
    try:
        el = page.css("title")
        if el:
            t = el[0].get_all_text()
            if t and t.strip():
                return t.strip()
    except Exception:
        pass
    return None


register(StealthTier())
