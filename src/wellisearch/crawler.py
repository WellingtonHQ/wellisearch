"""Native in-process crawler facade — the single crawl path.

Delegates to `crawl.engine.crawl` (policy → tier ladder → extractor → gate)
and keeps the historical public contract: `fit_markdown(url) -> (title, md)`,
`CrawlError`, `crawl_semaphore()`, and `health()`.
"""
from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .crawl.engine import crawl

log = logging.getLogger("wellisearch.crawler")

# Global crawl cap shared by the worker (queue drain + watchlist refresh)
# AND the fetch/refresh request paths: at most CRAWL_MAX_PARALLEL concurrent
# native crawls in total. fetch_pages used to fan out unbounded crawls per
# request, which could saturate the crawler and hold DB pool connections
# for the whole burst.
_crawl_sem: asyncio.Semaphore | None = None
_cf_crawl_sem: asyncio.Semaphore | None = None


def crawl_semaphore() -> asyncio.Semaphore:
    """Process-wide crawl concurrency cap (CRAWL_MAX_PARALLEL), created lazily."""
    global _crawl_sem
    if _crawl_sem is None:
        _crawl_sem = asyncio.Semaphore(get_settings().CRAWL_MAX_PARALLEL)
    return _crawl_sem


def cf_crawl_semaphore() -> asyncio.Semaphore:
    """CF-lane crawl concurrency cap (CRAWL_CHALLENGE_PARALLEL), created lazily.

    Kept separate from the fast-lane cap so a slow challenge crawl never holds
    a fast-lane slot (and vice versa).
    """
    global _cf_crawl_sem
    if _cf_crawl_sem is None:
        _cf_crawl_sem = asyncio.Semaphore(get_settings().CRAWL_CHALLENGE_PARALLEL)
    return _cf_crawl_sem


class CrawlError(Exception):
    """A failed crawl, carrying the URL, message, and optional HTTP status."""

    def __init__(
        self,
        url: str,
        message: str,
        status: int | None = None,
    ) -> None:
        """Keep the URL, message, and optional HTTP status on the failure."""
        super().__init__(f"{url}: {message}")
        self.url = url
        self.message = message
        self.status = status

    def status_label(self) -> str:
        """Short status for logs: ``http_<status>`` when present, else ``error``."""
        return f"http_{self.status}" if self.status else "error"


async def fit_markdown(url: str) -> tuple[str | None, str]:
    """Crawl one URL → (page title, clean fit-markdown). Raises CrawlError on failure.

    title is the page's <title> captured by the engine's extractor; None when
    the page has none — callers then store/keep no title.
    """
    result = await crawl(url)
    # result.ok is the success signal (the engine's gate passed). A failed crawl
    # can still carry a non-empty partial markdown (e.g. a bot-wall page with
    # some text); storing that as a success would poison the index, so require ok.
    if result.ok and result.md and result.md.strip():
        return result.title, result.md
    raise CrawlError(url, "all tiers failed or empty markdown")


async def health() -> tuple[bool, str]:
    """Wired-up check for /health: the native stack imports and the primary
    (browser) tier is registered. Fast and deterministic — no browser launch."""
    try:
        from .crawl import engine, tiers
    except Exception as e:
        return False, str(e)
    if tiers.by_name("browser") is None:
        return False, "browser tier not registered"
    return True, "ok"
