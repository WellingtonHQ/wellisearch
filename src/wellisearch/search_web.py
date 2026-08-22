"""search_web pipeline (plan §7): local index → gateway → log → enqueue → return.

No crawl in the response path: local hits cost zero provider credits; on a
miss the provider serves immediately and the top result URLs are enqueued
for background indexing (kicked, debounced).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from . import queue
from .config import get_settings
from .db import db
from .embed import embed_one
from .providers import GatewayExhausted, get_gateway

log = logging.getLogger("wellisearch.search_web")


def build_results_markdown(results: list[dict]) -> str:
    """The hard Markdown contract (plan §7): Title/URL/Snippet blocks,
    separated by `---` lines."""
    blocks = []
    for r in results:
        blocks.append(
            f"Title: {r.get('title') or r['url']}\n"
            f"URL: {r['url']}\n"
            f"Snippet: {r.get('snippet') or ''}"
        )
    return "\n---\n".join(blocks)


async def search_web(
    query: str,
    num_results: int | None = None,
    max_crawl: int | None = None,
    max_age_days: float | None = None,
) -> dict:
    s = get_settings()
    k = num_results or s.SEARCH_K
    crawl_n = s.SEARCH_MAX_CRAWL if max_crawl is None else max(0, max_crawl)

    # ---- local index first (zero provider cost)
    local_rows: list[dict] = []
    try:
        qvec = await asyncio.to_thread(embed_one, query)
    except Exception as e:
        log.warning("query embedding failed (%s) — searching with FTS+trigram only", e)
        qvec = None

    try:
        local_rows = await db.fetch_all(
            "SELECT * FROM fn_search_local(%s, %s::vector, %s)",
            (query, qvec if qvec is not None else None, k),
        )
    except Exception:
        log.exception("fn_search_local failed")
        local_rows = []

    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
        local_rows = [
            r for r in local_rows
            if r.get("last_crawled") is None or r["last_crawled"] >= cutoff
        ]

    def _local_result(r: dict) -> dict:
        return {
            "url": r["url"],
            "title": r.get("title") or r["url"],
            "snippet": (r.get("snippet") or "")[:400],
            "score": r.get("score"),
            "last_crawled": r.get("last_crawled"),
            "fetch_count": r.get("fetch_count"),
        }

    good_local = [r for r in local_rows if (r.get("score") or 0) >= s.SEARCH_MIN_SCORE]

    source: str
    results: list[dict]
    degraded = False
    errors: list[dict] = []

    if good_local:
        # local hit — zero provider credits (the quota-preservation layer)
        source = "local"
        results = [_local_result(r) for r in good_local[:k]]
        for r in results:
            await db.mark_search_hit(r["url"])
    else:
        # ---- provider gateway (no good local hit)
        gw = get_gateway()
        try:
            provider_results, provider_name, errors = await gw.search(query, k)
            source = provider_name
            results = [
                {
                    "url": r.url,
                    "title": r.title,
                    "snippet": r.snippet[:400],
                    "score": r.score,
                }
                for r in provider_results[:k]
            ]
            # speculative pre-indexing: enqueue top result URLs (background)
            for r in provider_results[:crawl_n]:
                await queue.enqueue(r.url, source="search")
        except GatewayExhausted as e:
            log.error("all providers failed: %s", e)
            errors = e.errors
            if local_rows:
                # degraded mode: serve whatever local results we have (§14.12)
                degraded = True
                source = "local"
                results = [_local_result(r) for r in local_rows[:k]]
                for r in results:
                    await db.mark_search_hit(r["url"])
            else:
                source = "error"
                results = []

    await db.log_search(query, source, len(good_local) if not degraded else len(local_rows), results)

    out: dict = {
        "results": build_results_markdown(results),
        "source": source,
        "degraded": degraded,
        "count": len(results),
    }
    if source == "local":
        out["last_crawled"] = [
            r["last_crawled"].isoformat() if r.get("last_crawled") else None for r in results
        ]
    if errors:
        out["provider_errors"] = errors
    return out
