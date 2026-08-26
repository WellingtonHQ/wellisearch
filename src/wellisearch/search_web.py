"""search_web pipeline (plan §7): local index → gateway → log → enqueue → return.

No crawl in the response path: local hits cost zero provider credits; on a
miss the provider serves immediately and the top result URLs are enqueued
for background indexing (kicked, debounced).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from . import queue
from .config import get_settings
from .db import db
from .embed import embed_one
from .providers import GatewayExhausted, get_gateway
from .serialize import format_timing

log = logging.getLogger("wellisearch.search_web")


def render_search_markdown(out: dict) -> str:
    """The search response as plain Markdown (no JSON envelope): a
    response-level header (Source / Degraded / Provider Errors) followed by
    Title/URL/Snippet blocks separated by `---` lines. Local hits carry a
    Last Crawled line per result."""
    lines = [
        f"Source: {out['source']}",
        f"Degraded: {'true' if out.get('degraded') else 'false'}",
    ]
    tline = format_timing(out.get("timing"))
    if tline:
        lines.append(tline)
    errors = out.get("provider_errors") or []
    if errors:
        lines.append(
            "Provider Errors: " + "; ".join(f"{e.get('provider')}: {e.get('error')}" for e in errors)
        )

    blocks = []
    for r in out.get("results") or []:
        block = [f"Title: {r.get('title') or r['url']}", f"URL: {r['url']}"]
        ts = r.get("last_crawled")
        if ts is not None:
            block.append(f"Last Crawled: {ts.isoformat() if hasattr(ts, 'isoformat') else ts}")
        block.append(f"Snippet: {r.get('snippet') or ''}")
        blocks.append("\n".join(block))

    if not blocks:
        return "\n".join(lines)
    return "\n\n".join(["\n".join(lines), "\n---\n".join(blocks)])


async def search_web(
    query: str,
    num_results: int | None = None,
    max_crawl: int | None = None,
    max_age_days: float | None = None,
    skip_local: bool = False,
) -> dict:
    s = get_settings()
    k = num_results or s.SEARCH_K
    crawl_n = s.SEARCH_MAX_CRAWL if max_crawl is None else max(0, max_crawl)

    t_start = time.monotonic()

    # ---- local index first (zero provider cost) — skipped entirely when
    # skip_local is set (the caller wants a live provider answer, e.g. after
    # being unsatisfied with a prior local result).
    local_rows: list[dict] = []
    index_ms = 0
    if not skip_local:
        t_index = time.monotonic()
        try:
            qvec = await asyncio.to_thread(embed_one, query)
        except Exception as e:
            log.warning("query embedding failed (%s) — searching with FTS+trigram only", e)
            qvec = None

        # Fetch a bit more than we'll return so the coverage gate (below) can
        # see a full-coverage page that ranks just outside the top-k by score.
        # The extra rows cost nothing — the legs/fusion are the same; only the
        # final LIMIT differs.
        gate_k = max(k, 10)
        try:
            local_rows = await db.fetch_all(
                "SELECT * FROM fn_search_local(%s, %s::vector, %s)",
                (query, qvec if qvec is not None else None, gate_k),
                timeout_ms=s.SEARCH_STATEMENT_TIMEOUT_MS,
            )
        except Exception:
            # includes QueryCanceled (statement timeout) — the gateway below
            # serves the request instead of stalling on the local index
            log.exception("fn_search_local failed (timeout_ms=%s)", s.SEARCH_STATEMENT_TIMEOUT_MS)
            local_rows = []

        if max_age_days is not None:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
            local_rows = [
                r for r in local_rows
                if r.get("last_crawled") is None or r["last_crawled"] >= cutoff
            ]
        index_ms = int((time.monotonic() - t_index) * 1000)

    # Gate: does any top local result cover the query enough? `coverage` is
    # computed in fn_search_local (see docs/ranking.md). With skip_local the
    # row set is empty, so the gate is always False and the gateway serves.
    serve_local = any(
        (r.get("coverage") or 0.0) >= s.LOCAL_MIN_COVERAGE for r in local_rows
    )

    def _local_result(r: dict) -> dict:
        return {
            "url": r["url"],
            "title": r.get("title") or r["url"],
            "snippet": (r.get("snippet") or "")[:400],
            "score": r.get("score"),
            "coverage": r.get("coverage"),
            "last_crawled": r.get("last_crawled"),
            "fetch_count": r.get("fetch_count"),
        }

    source: str
    results: list[dict]
    degraded = False
    errors: list[dict] = []
    provider_ms: int | None = None

    if serve_local:
        # local hit — zero provider credits (the quota-preservation layer)
        source = "local"
        results = [_local_result(r) for r in local_rows[:k]]
        for r in results:
            await db.mark_search_hit(r["url"])
    else:
        # ---- provider gateway (no good local hit)
        gw = get_gateway()
        t_prov = time.monotonic()
        try:
            provider_results, provider_name, errors = await gw.search(query, k)
            provider_ms = int((time.monotonic() - t_prov) * 1000)
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
            provider_ms = int((time.monotonic() - t_prov) * 1000)
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

    await db.log_search(query, source, len(results), results)

    # Envelope: structured data only. The Markdown body is rendered at the
    # surfaces (tools.py for MCP, app.py for REST) via render_search_markdown.
    timing: dict = {"total_ms": int((time.monotonic() - t_start) * 1000), "index_ms": index_ms}
    if provider_ms is not None:
        timing["provider_ms"] = provider_ms

    out: dict = {
        "results": results,
        "source": source,
        "degraded": degraded,
        "count": len(results),
        "timing": timing,
    }
    if errors:
        out["provider_errors"] = errors
    return out
