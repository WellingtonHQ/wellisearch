"""MCP tool surface — exactly the six tools (BLUEPRINT §7).

search_web   — search the web (local index → provider gateway)
fetch_page   — read one page (index or crawl-on-demand)
fetch_pages  — read many pages under a shared char budget
index_stats  — index + gateway snapshot
seed_url     — manually queue a URL for indexing
refresh_page — force an immediate re-crawl of one page
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import queue
from .config import get_settings
from .db import db
from .fetch import fetch_page as _fetch_page
from .fetch import fetch_pages as _fetch_pages
from .search_web import render_search_markdown
from .search_web import search_web as _search_web
from .truncation import STRATEGIES
from .worker import crawl_url


def _clean(obj: Any) -> Any:
    """JSON-safe: datetimes → ISO strings."""
    return json.loads(json.dumps(obj, default=str))


async def _index_stats_data() -> dict:
    s = get_settings()
    now = dt.datetime.now(dt.timezone.utc)

    pages = await db.fetch_one(
        "SELECT count(*) AS total, min(last_crawled) AS oldest, max(last_crawled) AS newest "
        "FROM pages"
    )
    chunks = await db.fetch_one("SELECT count(*) AS total FROM chunks")

    windows = {"24h": 1, "7d": 7, "30d": 30}
    trends: dict[str, dict[str, Any]] = {}
    for label, days in windows.items():
        rows = await db.fetch_all(
            "SELECT source, count(*) AS n FROM search_log "
            "WHERE ts >= now() - make_interval(days => %s) GROUP BY source",
            (days,),
        )
        total = sum(r["n"] for r in rows)
        by = {r["source"]: r["n"] for r in rows}
        trends[label] = {
            "total": total,
            "by_source": by,
            "hit_rate": {k: (v / total if total else 0.0) for k, v in by.items()},
        }

    q = await db.fetch_all("SELECT status, count(*) AS n FROM crawl_queue GROUP BY status")
    queue_depth = {
        "pending": 0, "in_flight": 0, "done": 0, "failed": 0,
        **{r["status"]: r["n"] for r in q},
    }
    oldest_pending = await db.fetch_one(
        "SELECT min(enqueued_at) AS oldest FROM crawl_queue WHERE status = 'pending'"
    )

    quota_rows = await db.fetch_all(
        "SELECT provider, used, quota_limit FROM provider_quota WHERE month = %s",
        (now.strftime("%Y-%m"),),
    )
    quota = []
    for r in quota_rows:
        limit = r["quota_limit"]
        if limit is None:
            limit = s.env_quota_limit(r["provider"])
        quota.append({
            "provider": r["provider"],
            "used": r["used"],
            "limit": limit,
            "pct": round(r["used"] / limit * 100, 1) if limit else None,
        })

    # crawl status mix (30d) for the trends panel
    crawls = await db.fetch_all(
        "SELECT status, count(*) AS n FROM crawl_log "
        "WHERE ts >= now() - interval '30 days' GROUP BY status"
    )

    return {
        "at": now.isoformat(),
        "index": {
            "pages": pages["total"],
            "chunks": chunks["total"],
            "oldest_crawl": pages["oldest"].isoformat() if pages["oldest"] else None,
            "newest_crawl": pages["newest"].isoformat() if pages["newest"] else None,
        },
        "search_trends": trends,
        "crawl_queue": {**queue_depth, "oldest_pending": oldest_pending["oldest"]},
        "quota_this_month": quota,
        "crawls_30d": {r["status"]: r["n"] for r in crawls},
    }


def register_tools(server: MCPServer) -> None:
    @server.tool(
        name="search_web",
        description=(
            "Search the web (local index first, provider gateway on a miss). "
            "Returns a Markdown document: a header with Source "
            "(local|tavily|brave|searxng|error) and Degraded (true|false), "
            "then result blocks of Title/URL/Snippet separated by --- lines. "
            "Local hits include a Last Crawled line per result and cost zero "
            "provider credits; on a miss, top result URLs are indexed in the "
            "background — no need to wait. If Degraded is true, all providers "
            "failed and only local results are shown (see the Provider Errors "
            "header line)."
        ),
    )
    async def search_web(
        query: str,
        num_results: int = 5,
        max_crawl: int = 5,
        max_age_days: float | None = None,
    ) -> str:
        out = await _search_web(
            query,
            num_results=num_results,
            max_crawl=max_crawl,
            max_age_days=max_age_days,
        )
        return render_search_markdown(out)

    @server.tool(
        name="fetch_page",
        description=(
            "Load one URL as clean/fit Markdown for reading. Returns stored "
            "content if indexed, else crawls on demand (and stores it). "
            "Bumps the page's fetch_count (priority + prominence)."
        ),
    )
    async def fetch_page(url: str, max_chars: int | None = None) -> dict:
        out = await _fetch_page(url, max_chars=max_chars)
        return _clean(out)

    @server.tool(
        name="fetch_pages",
        description=(
            "Bulk-read multiple URLs in one call under a shared total "
            "character budget. Returns combined Markdown, one clearly "
            "delimited section per page (URL/Title/---/content). "
            f"strategy: {list(STRATEGIES)} (default 'smart'). "
            "Each trimmed page carries a [truncated — N chars omitted, "
            "strategy=X] marker."
        ),
    )
    async def fetch_pages(
        urls: list[str],
        max_chars: int | None = None,  # null = full content (spec §7)
        per_page_chars: int | None = None,
        strategy: str = "smart",
    ) -> dict:
        out = await _fetch_pages(
            urls, max_chars=max_chars, per_page_chars=per_page_chars, strategy=strategy
        )
        return _clean(out)

    @server.tool(
        name="index_stats",
        description=(
            "Snapshot of the local index + provider gateway: page/chunk "
            "counts, freshness, search hit-rate by provider (24h/7d/30d), "
            "crawl queue depth, monthly quota usage vs limit. Use to gauge "
            "index freshness before relying on it."
        ),
    )
    async def index_stats() -> dict:
        return _clean(await _index_stats_data())

    @server.tool(
        name="seed_url",
        description=(
            "Manually add a URL to the index: queues a background crawl and "
            "kicks the worker. Returns queue position/status. Use to save a "
            "specific page for later retrieval."
        ),
    )
    async def seed_url(url: str) -> dict:
        from .fetch import _valid_url

        if not _valid_url(url):
            return {"ok": False, "url": url, "error": "invalid or non-http(s) url"}
        inserted = await queue.enqueue(url, source="manual")
        row = await db.fetch_one(
            "SELECT status, attempts, enqueued_at FROM crawl_queue WHERE url = %s "
            "ORDER BY enqueued_at DESC LIMIT 1",
            (url,),
        )
        pos = await db.fetch_one(
            "SELECT count(*) AS ahead FROM crawl_queue WHERE status = 'pending' "
            "AND enqueued_at < %s",
            ((row or {}).get("enqueued_at") or dt.datetime.now(dt.timezone.utc),),
        )
        return _clean({
            "ok": True,
            "url": url,
            "newly_queued": inserted,
            "queue": row,
            "ahead_in_queue": pos["ahead"],
        })

    @server.tool(
        name="refresh_page",
        description=(
            "Force an immediate re-crawl of a single page (bypasses the "
            "refresh-order priority). Returns the new last_crawled + status."
        ),
    )
    async def refresh_page(url: str) -> dict:
        from .fetch import _valid_url

        if not _valid_url(url):
            return {"ok": False, "url": url, "error": "invalid or non-http(s) url"}
        try:
            r = await crawl_url(url, trigger="manual")
        except Exception as e:
            page = await db.page_get(url)
            return _clean({
                "ok": False,
                "url": url,
                "error": str(e),
                "last_status": (page or {}).get("last_status"),
            })
        page = await db.page_get(url)
        return _clean({
            "ok": True,
            "url": url,
            "status": r.get("status"),
            "chunks": r.get("chunks"),
            "ms": r.get("ms"),
            "last_crawled": (page or {}).get("last_crawled"),
            "last_status": (page or {}).get("last_status"),
        })
