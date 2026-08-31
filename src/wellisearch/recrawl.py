"""One-shot: re-crawl every indexed page to refresh crawl-time values.

Stored fit_markdown is what a crawl produced; anything derived at crawl time
(the page's <title> today, an LLM cleanup pass later) cannot be recovered by
re-embedding or re-deriving from the stored markdown. This script re-crawls all
pages through the normal crawl_url() path (so the fresh values are stored via
store_page) with no worker budget cap.

Concurrency is the global crawl semaphore, set from CRAWL_MAX_PARALLEL. Run it
in a throwaway container instance (not the live app) so it doesn't fight the
worker for the same semaphore:

  CRAWL_MAX_PARALLEL=8 docker compose run --rm \
    -v "$PWD/src/wellisearch/recrawl.py:/usr/local/lib/python3.12/site-packages/wellisearch/recrawl.py:ro" \
    wellisearch python -m wellisearch.recrawl

Usage:
  python -m wellisearch.recrawl                 # re-crawl all pages
  python -m wellisearch.recrawl --limit 50      # first 50 (by fetch_count)
  python -m wellisearch.recrawl --dry-run       # report what would be re-crawled
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from .config import get_settings
from .db import db
from .worker import crawl_url

log = logging.getLogger("wellisearch.recrawl")

# Live-coroutine cap per gather batch. The global crawl semaphore (set from
# CRAWL_MAX_PARALLEL) is what actually bounds concurrent crawls; this just keeps
# the number of waiting coroutines bounded so a 20k-URL run doesn't hold 20k
# live tasks at once.
BATCH = 100
PREVIEW_LIMIT = 20  # dry-run: max URLs printed before "... and N more"


def main() -> None:
    """CLI entry point: parse args and run the one-shot re-crawl."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit", type=int, default=0,
        help="only re-crawl the first N (by fetch_count)"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="report what would be re-crawled, don't crawl"
    )
    ap.add_argument(
        "--resume", action="store_true",
        help="skip pages already crawled in the last 24h (resume a partially-finished run)"
    )
    args = ap.parse_args()
    asyncio.run(_run(limit=args.limit, dry_run=args.dry_run, resume=args.resume))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run(
    limit: int,
    dry_run: bool,
    resume: bool = False,
) -> None:
    """Select the target pages (optionally limited/resumable) and re-crawl
    them in BATCH-sized batches, reporting progress and stats."""
    s = get_settings()
    conc = s.CRAWL_MAX_PARALLEL
    await db.startup()
    try:
        q = ("SELECT url FROM pages "
             "WHERE fit_markdown IS NOT NULL AND disabled = false ")
        if resume:
            # Skip pages already refreshed in a prior run of this script: they
            # have a recent last_crawled. Pages that failed were never stored,
            # so their last_crawled is old and they stay in the set.
            q += "AND last_crawled < now() - interval '24 hours' "
        q += "ORDER BY fetch_count DESC, url"
        params: tuple = ()
        if limit:
            q += " LIMIT %s"
            params = (limit,)
        rows = await db.fetch_all(q, params)
        urls = [r["url"] for r in rows]
        total = len(urls)
        print(f"recrawl: {total} pages, concurrency={conc}", flush=True)
        if dry_run:
            for u in urls[:PREVIEW_LIMIT]:
                print(f"  {u}")
            if total > PREVIEW_LIMIT:
                print(f"  ... and {total - PREVIEW_LIMIT} more")
            return

        stats = {"ok": 0, "unchanged": 0, "failed": 0}
        t0 = time.monotonic()

        async def process(url: str) -> None:
            """Re-crawl one page via crawl_url and tally the outcome into stats."""
            try:
                r = await crawl_url(url, "recrawl")
                status = (r or {}).get("status", "ok")
            except Exception as e:  # keep going; one bad page shouldn't stop the run
                status = "error"
                log.warning("recrawl %s failed: %s", url, e)
            if status == "error":
                stats["failed"] += 1
            elif status == "unchanged":
                stats["unchanged"] += 1
            else:
                stats["ok"] += 1

        done = 0
        for i in range(0, total, BATCH):
            batch = urls[i:i + BATCH]
            await asyncio.gather(*(process(u) for u in batch))
            done += len(batch)
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed > 0 else 0.0
            eta_min = (total - done) / rate / 60 if rate > 0 else 0.0
            print(f"  {done}/{total} (ok={stats['ok']} unchanged={stats['unchanged']} "
                  f"failed={stats['failed']}) {rate:.1f}/s eta={eta_min:.0f}m", flush=True)

        elapsed = time.monotonic() - t0
        print(f"done in {elapsed / 60:.1f}m: ok={stats['ok']} "
              f"unchanged={stats['unchanged']} failed={stats['failed']}", flush=True)
    finally:
        await db.close()


if __name__ == "__main__":
    main()
