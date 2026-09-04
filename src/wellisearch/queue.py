"""crawl_queue: enqueue/dedupe + shared in-flight set + debounced kick.

- enqueue is deduped by the partial unique index (one pending/in_flight row
  per URL).
- The in-flight set (url → future) is shared across worker, fetch_page,
  fetch_pages and REST — the same URL is never crawled twice concurrently.
  It is in-memory; on boot, queue rows stuck in 'in_flight' reset to
  'pending' (db.queue_reset_in_flight).
- kick() is debounced (KICK_DEBOUNCE_S): a burst of enqueues coalesces into
  one worker tick.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from . import worker
from .config import get_settings
from .crawl.lane import CF, get_lane
from .crawler import crawl_semaphore, cf_crawl_semaphore
from .db import db

log = logging.getLogger("wellisearch.queue")


class InFlight:
    """Shared url → future map so the same URL is never crawled twice concurrently."""

    def __init__(self) -> None:
        """Starts with an empty url → future map."""
        self._m: dict[str, asyncio.Future] = {}

    def get(self, url: str) -> asyncio.Future | None:
        """The in-flight future for a URL, or None."""
        return self._m.get(url)

    def register(
        self,
        url: str,
        fut: asyncio.Future,
    ) -> None:
        """Record the in-flight future for a URL."""
        self._m[url] = fut

    def forget(self, url: str) -> None:
        """Remove a URL's entry (no-op when absent)."""
        self._m.pop(url, None)

    def urls(self) -> list[str]:
        """The URLs currently in flight."""
        return list(self._m.keys())

    def __len__(self) -> int:
        """The number of URLs currently in flight."""
        return len(self._m)


INFLIGHT = InFlight()


async def crawl_deduped(
    url: str,
    trigger: str,
    fn: Callable[[], Awaitable[object]],
) -> object:
    """Run `fn()` for url unless another crawl of the same URL is in flight;
    then await that crawl's result instead (no double-crawl).

    `fn` must be an async zero-arg callable returning the crawl+store result.
    """
    fut = INFLIGHT.get(url)
    if fut is not None and not fut.done():
        log.info("in-flight dedup: %s already crawling — awaiting shared result", url)
        return await asyncio.shield(fut)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    INFLIGHT.register(url, fut)
    try:
        # global crawl cap: dedup waiters above never hold a slot — only the
        # coroutine actually running the crawl does. The CF lane uses its own
        # (smaller) cap so a challenge crawl never holds a fast-lane slot.
        sem = cf_crawl_semaphore() if get_lane() == CF else crawl_semaphore()
        async with sem:
            result = await fn()
        if not fut.done():
            fut.set_result(result)
        return result
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
            # The owner coroutine re-raises this to its caller (which handles
            # it); a dedup waiter, if any, retrieves it via the future. Mark it
            # retrieved so asyncio doesn't log "Future exception was never
            # retrieved" when there is no waiter (the common queue-drain case).
            fut.exception()
        raise
    finally:
        INFLIGHT.forget(url)


async def enqueue(
    url: str,
    source: str = "search",
    kick: bool = True,
) -> bool:
    """Enqueue a URL for background crawling. Returns True if newly inserted."""
    inserted = await db.queue_enqueue(url, source)
    if inserted:
        log.info("queued %s (source=%s)", url, source)
        if kick:
            kick_worker()
    return inserted


# ---------------------------------------------------------------------------
# Kick State
# ---------------------------------------------------------------------------
_kick_task: asyncio.Task | None = None


def kick_worker() -> None:
    """Debounced kick: coalesce a burst of enqueues into one worker tick."""
    global _kick_task
    s = get_settings()
    if _kick_task is not None and not _kick_task.done():
        return  # debounce window already running

    async def _debounced() -> None:
        """Sleep out the debounce window, then run one worker tick."""
        await asyncio.sleep(s.KICK_DEBOUNCE_S)
        log.info("kicked worker tick (debounce %ss elapsed)", s.KICK_DEBOUNCE_S)
        await worker.tick()

    try:
        loop = asyncio.get_running_loop()
        _kick_task = loop.create_task(_debounced())
    except RuntimeError:
        # no running loop (e.g. unit test) — schedule a direct one
        asyncio.ensure_future(_debounced())
