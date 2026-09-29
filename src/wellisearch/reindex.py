"""Re-embed the entire index (BLUEPRINT §15).

Run after changing EMBED_MODEL (stored vectors are invalid) or to repair the
index. Iterates pages, re-chunks + re-embeds each (store_page's unchanged
short-circuit makes already-fresh pages a no-op).

Usage:
  python -m wellisearch.reindex            # reindex everything stale
  python -m wellisearch.reindex --force    # re-embed every page, even fresh
  python -m wellisearch.reindex --dry-run  # report only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from .config import get_settings
from .db import db
from .embed import model_name
from .index import store_page

log = logging.getLogger("wellisearch.reindex")

PROGRESS_INTERVAL = 10  # print progress every N pages


def main() -> None:
    """CLI entry point: parse args and run the re-embed."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-embed every page, even if fresh")
    ap.add_argument("--dry-run", action="store_true", help="report what would be re-embedded")
    args = ap.parse_args()
    asyncio.run(_run(force=args.force, dry_run=args.dry_run))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reembed_page(p: dict[str, Any]) -> str:
    """Re-embed one page; return 'ok', 'unchanged', or 'failed'."""
    url = p["url"]
    try:
        status, _ = await store_page(url, p["fit_markdown"], title=p["title"])
    except Exception as e:
        log.warning("reindex %s failed: %s", url, e)
        return "failed"
    return "unchanged" if status == "unchanged" else "ok"


async def _stale_count(force: bool, model: str) -> int:
    """Number of pages needing (re)embedding (all when force)."""
    row = await db.fetch_one(
        "SELECT count(*) AS n FROM pages WHERE fit_markdown IS NOT NULL "
        "AND (%s OR embedding_model IS DISTINCT FROM %s)",
        (force, model),
    )
    return int(row["n"]) if row else 0


async def _stale_batch(
    force: bool,
    model: str,
    after_url: str,
    limit: int,
) -> list[dict[str, Any]]:
    """One url-keyset page of stale rows (strictly after `after_url`).

    Filter in the DB, not the app: only rows needing (re)embedding are loaded
    (IS DISTINCT FROM also picks up rows with NULL embedding_model)."""
    return await db.fetch_all(
        "SELECT url, title, fit_markdown, content_hash, embedding_model "
        "FROM pages WHERE fit_markdown IS NOT NULL "
        "AND (%s OR embedding_model IS DISTINCT FROM %s) AND url > %s "
        "ORDER BY url LIMIT %s",
        (force, model, after_url, limit),
    )


async def _run(force: bool, dry_run: bool) -> None:
    """Re-chunk + re-embed every page needing it (all when --force).

    Pages are processed in url-keyset batches of REINDEX_BATCH_SIZE so only
    one batch of fit_markdown is resident at a time — peak memory stays flat
    no matter how large the index is."""
    s = get_settings()
    await db.startup()
    try:
        total = await db.fetch_one(
            "SELECT count(*) AS n FROM pages WHERE fit_markdown IS NOT NULL"
        )
        stale_n = await _stale_count(force, s.EMBED_MODEL)
        print(
            f"index: {total['n']} pages; to (re)embed: {stale_n} "
            f"(model={model_name()}, EMBED_DIMS={s.EMBED_DIMS})"
        )
        if dry_run or stale_n == 0:
            return

        stats = {"failed": 0, "ok": 0, "unchanged": 0}
        done = 0
        last_url = ""
        while True:
            batch = await _stale_batch(force, s.EMBED_MODEL, last_url, s.REINDEX_BATCH_SIZE)
            if not batch:
                break
            for p in batch:
                outcome = await _reembed_page(p)
                stats[outcome] += 1
                done += 1
                if done % PROGRESS_INTERVAL == 0 or done == stale_n:
                    print(
                        f"  {done}/{stale_n} (ok={stats['ok']} unchanged={stats['unchanged']} "
                        f"failed={stats['failed']})"
                    )
            last_url = batch[-1]["url"]

        print(f"done: ok={stats['ok']} unchanged={stats['unchanged']} failed={stats['failed']}")
    finally:
        await db.close()


if __name__ == "__main__":
    main()
