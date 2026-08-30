"""Background worker (asyncio task in the app; plan §8).

Two jobs per tick:
  1. drain crawl_queue (search/manual enqueues) — up to the per-tick budget,
     CRAWL_MAX_PARALLEL at a time → Crawl4AI /md → store_page → done/failed
     (transient errors re-enqueued up to QUEUE_MAX_ATTEMPTS).
  2. refresh watchlist — only pages crawled > REFRESH_MIN_AGE_HOURS ago
     (or never), ORDER BY fetch_count DESC, last_crawled ASC
     LIMIT WORKER_BUDGET_PER_RUN → crawl → unchanged? skip re-embed.

Tick triggers:
  - every WORKER_INTERVAL_MIN unconditionally
  - debounced kick whenever the queue receives items (queue.kick_worker)
  - per-tick wall-clock budget WORKER_TICK_BUDGET_MIN

`python -m wellisearch.worker --once` runs one tick and exits (manual runs).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from . import crawler, queue
from .config import get_settings
from .db import db
from .index import store_page

log = logging.getLogger("wellisearch.worker")

ERROR_DETAIL_MAX_LEN = 1000  # max chars kept in a crawl error detail (crawl_log)
ERROR_REPR_MAX_LEN = 500     # max chars kept in a crash repr (crawl_log)

# runtime state for the dashboard "Now" panel
STATE: dict = {
    "last_tick_at": None,
    "last_tick_stats": None,
    "started_at": None,
}

# One tick at a time: the interval timer (run_forever) and debounced kicks
# (queue.kick_worker) can otherwise run tick() concurrently, doubling crawl
# load. Claims are atomic either way, so the lock is about load, not races.
_tick_lock = asyncio.Lock()


async def crawl_url(url: str, trigger: str) -> dict:
    """Public entry: crawl one URL, never twice concurrently (shared set)."""
    return await queue.crawl_deduped(url, trigger, lambda: _crawl_and_store(url, trigger))


async def tick() -> dict:
    """One worker tick: drain queue + budgeted refresh, wall-clock bounded.
    Skipped (not queued) if a tick is already running."""
    if _tick_lock.locked():
        log.info("tick skipped (previous tick still running)")
        return {"skipped": "tick already running"}
    async with _tick_lock:
        s = get_settings()
        t0 = time.monotonic()
        deadline = t0 + s.WORKER_TICK_BUDGET_MIN * 60
        log.info("tick start (budget %ss)", int(deadline - t0))

        stats = {
            "queue": await _drain_queue(deadline),
            "refresh": await _refresh_watchlist(deadline),
            "ms": int((time.monotonic() - t0) * 1000),
        }
        STATE["last_tick_at"] = dt.datetime.now(dt.timezone.utc)
        STATE["last_tick_stats"] = stats
        log.info("tick done: %s", stats)
        await _log_event("worker tick", stats)
        await _retention_sweep()
        return stats


async def run_forever() -> None:
    """Worker loop: periodic ticks + reacts to kicks (which call tick()
    directly, so the loop only needs the interval timer)."""
    s = get_settings()
    STATE["started_at"] = dt.datetime.now(dt.timezone.utc)
    log.info(
        "worker started (interval=%sm budget/run=%d parallel=%d)",
        s.WORKER_INTERVAL_MIN, s.WORKER_BUDGET_PER_RUN, s.CRAWL_MAX_PARALLEL,
    )
    while True:
        await asyncio.sleep(s.WORKER_INTERVAL_MIN * 60)
        try:
            await tick()
        except Exception as e:
            log.exception("worker tick crashed")
            await _log_event("worker tick crashed", {"error": repr(e)[:500]})


async def run_once() -> dict:
    """--once mode: drain queue + one refresh pass, then exit."""
    await db.queue_reset_in_flight()
    return await tick()


def main() -> None:
    """CLI entry point: run one tick (--once) and exit."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if "--once" in sys.argv:
        result = asyncio.run(_once())
        print(result)
    else:
        print("worker --once not given; run `python -m wellisearch.worker --once` "
              "for a manual run (the app starts the worker itself).", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _crawl_and_store(url: str, trigger: str) -> dict:
    """One crawl+store attempt (in-flight-deduped by the caller)."""
    t0 = time.monotonic()
    ms = 0
    try:
        title, md = await crawler.fit_markdown(url)
    except crawler.CrawlError as e:
        ms = int((time.monotonic() - t0) * 1000)
        label = e.status_label()
        await db.log_crawl(url, trigger, label, ms, detail=e.message[:ERROR_DETAIL_MAX_LEN])
        await db.execute(
            "UPDATE pages SET last_status = %s WHERE url = %s",
            (label, url),
        )
        raise
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        await db.log_crawl(url, trigger, "error", ms, detail=repr(e)[:ERROR_REPR_MAX_LEN])
        raise

    try:
        status, chunks_written = await store_page(url, md, title=title)
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        await db.log_crawl(url, trigger, "error", ms, detail=f"store: {e!r}"[:500])
        raise

    ms = int((time.monotonic() - t0) * 1000)
    await db.log_crawl(url, trigger, status, ms, chunks_written=chunks_written)
    log.info("crawl %s: %s (%d ms, %d chunks)", url, status, ms, chunks_written)
    return {"url": url, "status": status, "ms": ms, "chunks": chunks_written}


async def _drain_queue(deadline: float) -> dict:
    """Claim and crawl pending queue rows, up to the per-tick budget."""
    s = get_settings()
    processed = 0
    sem = asyncio.Semaphore(s.CRAWL_MAX_PARALLEL)

    rows = await db.fetch_all(
        "SELECT url FROM crawl_queue WHERE status = 'pending' "
        "ORDER BY enqueued_at LIMIT %s",
        (s.WORKER_BUDGET_PER_RUN * 2,),
    )
    log.info("tick: draining queue (%d pending in budget window)", len(rows))

    async def process(url: str) -> None:
        """Claim one queue row and crawl it (bounded by the parallelism semaphore)."""
        nonlocal processed
        if time.monotonic() > deadline:
            return
        if not await db.queue_claim(url):
            return
        async with sem:
            try:
                await crawl_url(url, "search")
                await db.queue_done(url, ok=True)
            except Exception as e:
                log.warning("queue crawl failed for %s: %s", url, e)
                await db.queue_done(url, ok=False, error=str(e)[:1000])
            finally:
                processed += 1

    await asyncio.gather(*(process(r["url"]) for r in rows))
    return {"processed": processed}


async def _refresh_watchlist(deadline: float) -> dict:
    """Refresh stale watchlist pages (by fetch_count), up to the per-tick budget."""
    s = get_settings()
    min_age = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=s.REFRESH_MIN_AGE_HOURS)
    rows = await db.fetch_all(
        "SELECT url, fetch_count FROM pages WHERE disabled = false "
        "AND (last_crawled IS NULL OR last_crawled < %s) "
        "ORDER BY fetch_count DESC, last_crawled ASC LIMIT %s",
        (min_age, s.WORKER_BUDGET_PER_RUN),
    )
    if not rows:
        return {"refreshed": 0}
    log.info("tick: refresh watchlist (%d pages)", len(rows))
    sem = asyncio.Semaphore(s.CRAWL_MAX_PARALLEL)
    results = []

    async def refresh(row: dict) -> None:
        """Re-crawl one watchlist page and record the result."""
        if time.monotonic() > deadline:
            return
        url = row["url"]
        try:
            r = await crawl_url(url, "refresh")
            results.append(r)
        except Exception as e:
            log.warning("refresh failed for %s: %s", url, e)
            results.append({"url": url, "status": "error", "error": str(e)[:200]})

    await asyncio.gather(*(refresh(r) for r in rows))
    unchanged = sum(1 for r in results if r.get("status") == "unchanged")
    return {"refreshed": len(results), "unchanged": unchanged}


async def _log_event(message: str, info: dict | None = None) -> None:
    """Best-effort: event logging must never break the worker."""
    try:
        await db.log_event(message, info)
    except Exception as e:
        log.warning("event logging failed: %s", e)


async def _retention_sweep() -> None:
    """Prune old log rows past the retention window (best-effort)."""
    try:
        s = get_settings()
        pruned = await db.prune_logs(s.LOG_RETENTION_DAYS)
        total = sum(pruned.values())
        if total:
            await _log_event("log retention sweep", {"retention_days": s.LOG_RETENTION_DAYS, **pruned})
            log.info("pruned %d old log rows", total)
    except Exception as e:
        log.warning("retention sweep failed: %s", e)


async def _once() -> dict:
    """One-shot mode: open the DB, run a single tick, and close it."""
    from .db import db as _db

    await _db.startup()
    try:
        return await run_once()
    finally:
        await _db.close()


if __name__ == "__main__":
    main()
