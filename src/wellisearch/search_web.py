"""search_web pipeline (plan §7): local index → gateway → log → enqueue → return.

No crawl in the response path: local hits cost zero provider credits; on a
miss the provider serves immediately and the top result URLs are enqueued
for background indexing (kicked, debounced). `search_mode` selects the
source: auto (local first, default), local (index only), provider (gateway
only).
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

# search_mode values: which source serves the answer.
#   auto     — local index first, provider gateway on a miss (default)
#   local    — local index only; an error if the index has nothing
#   provider — provider gateway only; the local index is not consulted
SEARCH_MODES = ("auto", "local", "provider")


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
    if out.get("index_error"):
        lines.append(f"Index Error: {out['index_error']}")

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


async def _search_local_index(query: str, k: int, max_age_days: float | None) -> tuple[list[dict], int, str | None]:
    """The local-index leg: embed the query, rank via fn_search_local, apply
    the optional freshness filter. Returns (rows, index_ms, index_error).

    A bad query embedding degrades to FTS+trigram only (not an error). A
    failed fn_search_local (statement timeout, DB error) degrades to an empty
    row set AND returns the exception as `index_error` — auto mode falls back
    to the provider gateway (the error is hidden there), local mode surfaces
    it in the error envelope so "the index is empty" and "the index is down"
    are distinguishable.
    """
    s = get_settings()
    t_index = time.monotonic()
    try:
        qvec = await asyncio.to_thread(embed_one, query)
    except Exception as e:
        log.warning("query embedding failed (%s) — searching with FTS+trigram only", e)
        qvec = None

    # Fetch a bit more than we'll return so the coverage gate can see a
    # full-coverage page that ranks just outside the top-k by score. The extra
    # rows cost nothing — the legs/fusion are the same; only the final LIMIT
    # differs.
    gate_k = max(k, 10)
    try:
        rows = await db.fetch_all(
            "SELECT * FROM fn_search_local(%s, %s::vector, %s)",
            (query, qvec if qvec is not None else None, gate_k),
            timeout_ms=s.SEARCH_STATEMENT_TIMEOUT_MS,
        )
    except Exception as e:
        log.exception("fn_search_local failed (timeout_ms=%s)", s.SEARCH_STATEMENT_TIMEOUT_MS)
        return [], int((time.monotonic() - t_index) * 1000), f"{type(e).__name__}: {e}"

    if max_age_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
        rows = [
            r for r in rows
            if r.get("last_crawled") is None or r["last_crawled"] >= cutoff
        ]
    return rows, int((time.monotonic() - t_index) * 1000), None


async def search_web(
    query: str,
    num_results: int | None = None,
    max_crawl: int | None = None,
    max_age_days: float | None = None,
    search_mode: str = "auto",
) -> dict:
    s = get_settings()
    k = max(1, num_results or s.SEARCH_K)  # clamp: negative k would slice rows off the end
    crawl_n = s.SEARCH_MAX_CRAWL if max_crawl is None else max(0, max_crawl)
    if search_mode not in SEARCH_MODES:
        raise ValueError(
            f"invalid search_mode {search_mode!r} (choose from {list(SEARCH_MODES)})"
        )

    t_start = time.monotonic()

    # ---- local index (zero provider cost) — skipped entirely in provider
    # mode (the caller wants a live provider answer, e.g. after being
    # unsatisfied with a prior local result).
    local_rows: list[dict] = []
    index_ms = 0
    index_error: str | None = None
    if search_mode != "provider":
        local_rows, index_ms, index_error = await _search_local_index(query, k, max_age_days)

    # Gate: does any top local result cover the query enough? `coverage` is
    # computed in fn_search_local (see docs/ranking.md). In provider mode the
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

    if search_mode == "local":
        # local only: serve what the index has — the caller explicitly chose
        # local, so the coverage gate does not apply. No provider fallback.
        if local_rows:
            source = "local"
            results = [_local_result(r) for r in local_rows[:k]]
            for r in results:
                await db.mark_search_hit(r["url"])
        else:
            source = "error"
            results = []
    elif serve_local:
        # local hit — zero provider credits (the quota-preservation layer)
        source = "local"
        results = [_local_result(r) for r in local_rows[:k]]
        for r in results:
            await db.mark_search_hit(r["url"])
    else:
        # ---- provider gateway (auto: no good local hit; provider: always)
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
            if search_mode == "auto" and local_rows:
                # degraded mode: serve whatever local results we have (§14.12)
                degraded = True
                source = "local"
                results = [_local_result(r) for r in local_rows[:k]]
                for r in results:
                    await db.mark_search_hit(r["url"])
            else:
                # provider mode has no local fallback; auto only reaches here
                # when the index returned nothing
                source = "error"
                results = []

    await db.log_search(query, source, len(results), results)

    # Envelope: structured data only. The Markdown body is rendered at the
    # surfaces (tools.py for MCP, app.py for REST) via render_search_markdown.
    # index_ms is only present when the index leg actually ran — provider
    # mode never consults the index, so `index: 0 ms` would mislead.
    timing: dict = {"total_ms": int((time.monotonic() - t_start) * 1000)}
    if search_mode != "provider":
        timing["index_ms"] = index_ms
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
    # local mode with a failed index leg: the empty row set is a failure, not
    # "nothing indexed" — carry the diagnostics in the envelope (mirrors
    # provider_errors) so the caller can tell the two apart.
    if source == "error" and index_error:
        out["index_error"] = index_error
    return out
