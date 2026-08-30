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

from .config import get_settings
from .db import db
from .index import store_page
from .embed import model_name


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-embed every page, even if fresh")
    ap.add_argument("--dry-run", action="store_true", help="report what would be re-embedded")
    args = ap.parse_args()
    asyncio.run(_run(force=args.force, dry_run=args.dry_run))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run(force: bool, dry_run: bool) -> None:
    s = get_settings()
    await db.startup()
    try:
        total = await db.fetch_one(
            "SELECT count(*) AS n FROM pages WHERE fit_markdown IS NOT NULL"
        )
        # Filter in the DB, not the app: only rows needing (re)embedding are
        # loaded (IS DISTINCT FROM also picks up rows with NULL embedding_model).
        stale = await db.fetch_all(
            "SELECT url, title, fit_markdown, content_hash, embedding_model "
            "FROM pages WHERE fit_markdown IS NOT NULL "
            "AND (%s OR embedding_model IS DISTINCT FROM %s) "
            "ORDER BY fetch_count DESC",
            (force, s.EMBED_MODEL, s.EMBED_MODEL),
        )
        print(f"index: {total['n']} pages; to (re)embed: {len(stale)} "
              f"(model={model_name()}, EMBED_DIMS={s.EMBED_DIMS})")
        if dry_run:
            return

        ok = unchanged = failed = 0
        for i, p in enumerate(stale, 1):
            url = p["url"]
            try:
                status, chunks = await store_page(url, p["fit_markdown"], title=p["title"])
            except Exception as e:
                failed += 1
                logging.getLogger("wellisearch.reindex").error("reindex %s failed: %s", url, e)
                continue
            if status == "unchanged":
                unchanged += 1
            else:
                ok += 1
            if i % 10 == 0 or i == len(stale):
                print(f"  {i}/{len(stale)} (ok={ok} unchanged={unchanged} failed={failed})")

        print(f"done: ok={ok} unchanged={unchanged} failed={failed}")
    finally:
        await db.close()


if __name__ == "__main__":
    main()
