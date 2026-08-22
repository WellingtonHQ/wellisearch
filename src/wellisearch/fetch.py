"""fetch_page (single) + fetch_pages (bulk, budgeted) — the authoritative
on-demand read path (plan §7).

- Returns stored fit_markdown if indexed, else crawls on demand via Crawl4AI
  and stores it (the read path is the real indexing loop).
- Bumps fetch_count for every page fetched (priority + prominence).
- Never crawls a URL twice concurrently (shared in-flight set).
- fetch_pages allocates a shared char budget with swappable, boundary-safe
  truncation strategies (truncation.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from . import crawler
from .config import get_settings
from .db import db
from .index import store_page
from .truncation import (
    STRATEGIES,
    allocate_budgets,
    truncate_page,
    truncation_marker,
)
from .worker import crawl_url

log = logging.getLogger("wellisearch.fetch")

_OMITTED = object()  # sentinel: "parameter not provided"


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _title_from_markdown(md: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    if m:
        return m.group(1).strip()
    for line in md.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return None


async def _resolve_page(url: str) -> dict:
    """Content for one URL: from index when present, else crawl on demand."""
    page = await db.page_get(url)
    if page and not page.get("disabled") and page.get("fit_markdown"):
        return {
            "url": url,
            "title": page.get("title") or _title_from_markdown(page["fit_markdown"]) or url,
            "content": page["fit_markdown"],
            "from_index": True,
            "fetch_count": page.get("fetch_count") or 0,
        }

    # crawl on demand (in-flight-deduped inside crawl_url)
    r = await crawl_url(url, trigger="fetch")
    page = await db.page_get(url)
    md = (page or {}).get("fit_markdown") or ""
    if not md:
        raise RuntimeError(f"crawl succeeded but no content stored for {url}")
    return {
        "url": url,
        "title": (page or {}).get("title") or _title_from_markdown(md) or url,
        "content": md,
        "from_index": False,
        "fetch_count": (page or {}).get("fetch_count") or 0,
    }


async def fetch_page(url: str, max_chars: int | None = None) -> dict:
    """Single-URL read: stored or crawled-on-demand, fetch_count bumped."""
    if not _valid_url(url):
        return {"ok": False, "error": f"invalid or non-http(s) url: {url!r}", "url": url}

    try:
        page = await _resolve_page(url)
    except Exception as e:
        log.warning("fetch_page failed for %s: %s", url, e)
        return {"ok": False, "error": str(e), "url": url}

    await db.bump_fetch_count(url)

    truncated = False
    omitted = 0
    text = page["content"]
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        text = truncate_page(text, max_chars, "head")[0]
        truncated = True
        omitted = len(page["content"]) - len(text)
        text = text + "\n" + truncation_marker(omitted, "head")

    return {
        "ok": True,
        "url": url,
        "title": page["title"],
        "markdown": text,
        "chars": len(text),
        "truncated": truncated,
        "from_index": page["from_index"],
    }


async def fetch_pages(
    urls: list[str],
    max_chars: int | None = _OMITTED,  # type: ignore[assignment]
    per_page_chars: int | None = _OMITTED,  # type: ignore[assignment]
    strategy: str | None = None,
) -> dict:
    """Bulk read under a shared char budget with a swappable strategy."""
    s = get_settings()

    # --- validate + dedupe (preserve first-seen order)
    seen: set[str] = set()
    clean: list[str] = []
    bad: list[dict] = []
    for u in urls or []:
        if not isinstance(u, str) or not _valid_url(u):
            bad.append({"url": str(u), "error": "invalid or non-http(s) url"})
            continue
        if u not in seen:
            seen.add(u)
            clean.append(u)

    if not clean:
        return {
            "ok": False,
            "error": "no valid urls provided",
            "pages": bad,
        }

    strat = (strategy or s.FETCH_DEFAULT_STRATEGY).lower()
    if strat not in STRATEGIES:
        return {
            "ok": False,
            "error": f"unknown strategy {strat!r} (choose from {list(STRATEGIES)})",
            "pages": bad,
        }

    budget: int | None
    if max_chars is _OMITTED:
        budget = s.FETCH_MAX_CHARS or None
    else:
        budget = max_chars if max_chars and max_chars > 0 else None  # 0/None = unlimited

    per_page: int | None
    if per_page_chars is _OMITTED:
        per_page = s.FETCH_PER_PAGE_CHARS or None
    else:
        per_page = per_page_chars if per_page_chars and per_page_chars > 0 else None

    # --- resolve all pages in parallel (in-flight-deduped)
    resolved: list[dict] = []
    failed: list[dict] = []

    async def _one(u: str) -> None:
        try:
            resolved.append(await _resolve_page(u))
        except Exception as e:
            log.warning("fetch_pages: %s failed: %s", u, e)
            failed.append({"url": u, "error": str(e)[:300]})

    await asyncio.gather(*(_one(u) for u in clean))
    if not resolved:
        return {
            "ok": False,
            "error": "all urls failed to fetch",
            "pages": failed + bad,
        }

    # bump fetch_count for every successfully fetched page
    for p in resolved:
        await db.bump_fetch_count(p["url"])

    # --- allocate the budget per strategy
    lens = [len(p["content"]) for p in resolved]
    weights = [p["fetch_count"] for p in resolved]
    budgets = allocate_budgets(strat, lens, weights, budget, per_page)

    sections: list[str] = []
    page_meta: list[dict] = []
    total_chars = 0
    any_truncated = False

    for p, chars in zip(resolved, budgets):
        text, truncated = truncate_page(p["content"], chars, strat)
        omitted = len(p["content"]) - len(text)
        if truncated:
            text = text + "\n" + truncation_marker(omitted, strat)
            any_truncated = True
        section = (
            f"URL: {p['url']}\n"
            f"Title: {p['title']}\n"
            f"---\n"
            f"{text}"
        )
        sections.append(section)
        total_chars += len(section)
        page_meta.append({
            "url": p["url"],
            "chars_used": len(text),
            "truncated": truncated,
            "omitted": omitted if truncated else 0,
            "from_index": p["from_index"],
        })

    return {
        "ok": True,
        "markdown": "\n\n".join(sections),
        "total_chars": total_chars,
        "pages_fetched": len(resolved),
        "truncated": any_truncated,
        "strategy": strat,
        "budget": budget,
        "pages": page_meta + failed + bad,
    }
