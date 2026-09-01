"""Smoke tests: the three transport tiers against a live URL (design §3.1).

Exercises curl_cffi (http), patchright headful on Xvfb (browser), and
Scrapling StealthySession (stealth), plus the browser pool. Run inside the
Docker image (needs network + Xvfb + chromium).
"""
from __future__ import annotations

import asyncio

from wellisearch.crawl.policy import match
from wellisearch.crawl.pool import get_pool
from wellisearch.crawl.tiers.browser import BrowserTier
from wellisearch.crawl.tiers.http import HttpTier
from wellisearch.crawl.tiers.stealth import StealthTier

URL = "https://python.langchain.com/docs/introduction/"


async def main() -> None:
    p = match(URL)

    # ---------------------------------------------------------------------------
    # Http Tier
    # ---------------------------------------------------------------------------
    r = await HttpTier().fetch(URL, p)
    assert r.status == 200, r.status
    assert r.html, "empty html"
    print("OK http tier")

    # ---------------------------------------------------------------------------
    # Browser Tier
    # ---------------------------------------------------------------------------
    r = await BrowserTier().fetch(URL, p)
    assert r.html, "empty html"
    assert r.title, "empty title"
    print("OK browser tier")

    # ---------------------------------------------------------------------------
    # Stealth Tier
    # ---------------------------------------------------------------------------
    r = await StealthTier().fetch(URL, p)
    assert r.html, "empty html"
    print("OK stealth tier")

    # ---------------------------------------------------------------------------
    # Pool
    # ---------------------------------------------------------------------------
    ctx = await get_pool().acquire("shared")
    await get_pool().release(ctx)
    await get_pool().close_all()
    print("OK pool")

    print("ALL TIER SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
