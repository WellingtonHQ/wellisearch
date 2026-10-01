"""Collapse pages stored under variant URLs of the same page (urlnorm).

normalize_url prevents new duplicates at store/enqueue time, but rows already
stored under tracking-parameter / slug / locale variants remain. This tool
groups pages by canonical URL, keeps one row per group (the most-crawled row
with content), renames it to the canonical form, and deletes the rest —
chunks cascade via FK.

The rename is a parent/child key change on both sides of an FK, so the run
drops chunks_url_fkey for the duration of one transaction and re-adds it at
the end; if any orphaned chunk remains, the whole run rolls back.

Usage:
  python -m wellisearch.merge_dupes            # merge in place
  python -m wellisearch.merge_dupes --dry-run  # report only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from psycopg import AsyncConnection

from .db import db
from .urlnorm import normalize_url

log = logging.getLogger("wellisearch.merge_dupes")

CHUNKS_FK_NAME = "chunks_url_fkey"


def main() -> None:
    """CLI entry point: parse args and run the merge."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report what would be merged")
    args = ap.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _survivor(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The row to keep for one canonical group: has content, most crawled."""
    return sorted(
        rows,
        key=lambda r: (r["md_len"] is None, -r["fetch_count"], -(r["md_len"] or 0), r["url"]),
    )[0]


async def _run(dry_run: bool) -> None:
    """Group pages by canonical URL and collapse each group to one row."""
    await db.startup()
    try:
        rows = await db.fetch_all(
            "SELECT url, fetch_count, length(fit_markdown) AS md_len FROM pages"
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(normalize_url(r["url"]), []).append(r)
        pending = {
            canonical: group for canonical, group in sorted(groups.items())
            if len(group) > 1 or group[0]["url"] != canonical
        }

        total_renamed = 0
        total_deleted = 0
        if dry_run:
            _report_groups(pending)
        else:
            total_renamed, total_deleted = await _merge_groups(pending)

        print(
            f"{len(rows)} pages in {len(groups)} canonical groups; "
            f"{'would merge' if dry_run else 'merged'} {len(pending)} groups "
            f"(renamed={total_renamed} deleted={total_deleted})"
        )
    finally:
        await db.close()


def _report_groups(groups: dict[str, list[dict[str, Any]]]) -> None:
    """Log the survivor of each pending group without changing stored rows."""
    for canonical, group in groups.items():
        keep = _survivor(group)
        log.info(
            "would merge %d variants -> %s (keep fetch_count=%d)",
            len(group), canonical, keep["fetch_count"],
        )


async def _merge_groups(groups: dict[str, list[dict[str, Any]]]) -> tuple[int, int]:
    """Merge all groups atomically, validating the restored chunk FK before commit."""
    total_renamed = 0
    total_deleted = 0
    async with db.transaction() as conn:
        await conn.execute(f"ALTER TABLE chunks DROP CONSTRAINT IF EXISTS {CHUNKS_FK_NAME}")
        for canonical, group in groups.items():
            renamed, deleted = await _merge_group(conn, canonical, group)
            total_renamed += renamed
            total_deleted += deleted
        await conn.execute(
            f"ALTER TABLE chunks ADD CONSTRAINT {CHUNKS_FK_NAME} "
            "FOREIGN KEY (url) REFERENCES pages(url) ON DELETE CASCADE"
        )
    return total_renamed, total_deleted


async def _merge_group(
    conn: AsyncConnection,
    canonical: str,
    group: list[dict[str, Any]],
) -> tuple[int, int]:
    """Delete losing variants and rename the survivor and its chunks together."""
    keep = _survivor(group)
    drop = [r["url"] for r in group if r["url"] != keep["url"]]
    for url in drop:
        # The FK is temporarily dropped, so cascade deletion is unavailable.
        await conn.execute("DELETE FROM chunks WHERE url = %s", (url,))
        await conn.execute("DELETE FROM pages WHERE url = %s", (url,))
    renamed = int(keep["url"] != canonical)
    if renamed:
        await conn.execute("UPDATE chunks SET url = %s WHERE url = %s", (canonical, keep["url"]))
        await conn.execute("UPDATE pages SET url = %s WHERE url = %s", (canonical, keep["url"]))
    log.info(
        "merged %d variants -> %s (keep fetch_count=%d)",
        len(group), canonical, keep["fetch_count"],
    )
    return renamed, len(drop)


if __name__ == "__main__":
    main()
