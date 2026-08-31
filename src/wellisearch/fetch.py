"""fetch_page (single) + fetch_pages (bulk, budgeted) — the authoritative
on-demand read path (plan §7).

- Returns stored fit_markdown if indexed, else crawls on demand via the
  native crawler and stores it (the read path is the real indexing loop).
- Bumps fetch_count for every page fetched (priority + prominence).
- Never crawls a URL twice concurrently (shared in-flight set).
- fetch_pages allocates a shared char budget with swappable, boundary-safe
  truncation strategies (truncation.py).
- The pipelines return structured dicts; render_fetch_page_markdown /
  render_fetch_pages_markdown turn them into the plain-Markdown responses
  served by the MCP + REST surfaces (no JSON envelope).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

from . import crawler
from .config import get_settings
from .db import db
from .serialize import format_timing
from .truncation import (
    STRATEGIES,
    allocate_budgets,
    truncate_page,
    truncation_marker,
)
from .worker import crawl_url

log = logging.getLogger("wellisearch.fetch")

TITLE_MAX_LEN = 120  # max chars kept when deriving a title from the first line
ERROR_MAX_LEN = 300  # max chars kept in a per-URL fetch error message

_OMITTED = object()  # sentinel: "parameter not provided"


def render_fetch_page_markdown(out: dict) -> str:
    """The fetch_page response as plain Markdown (no JSON envelope): a
    Title/URL/From Index/Chars/Truncated header followed by the page body.
    A failed fetch is a URL/Status/Error header only."""
    if not out.get("ok"):
        lines = [
            f"URL: {out.get('url') or ''}",
            "Status: failed",
            f"Error: {out.get('error') or 'unknown error'}",
        ]
        tline = format_timing(out.get("timing"))
        if tline:
            lines.append(tline)
        return "\n".join(lines)
    lines = [
        f"Title: {out.get('title') or out.get('url') or ''}",
        f"URL: {out.get('url') or ''}",
        f"From Index: {'true' if out.get('from_index') else 'false'}",
        f"Chars: {out.get('chars') or 0}",
        f"Truncated: {'true' if out.get('truncated') else 'false'}",
    ]
    tline = format_timing(out.get("timing"))
    if tline:
        lines.append(tline)
    return "\n\n".join(["\n".join(lines), out.get("markdown") or ""])


def render_fetch_pages_markdown(out: dict) -> str:
    """The fetch_pages response as plain Markdown (no JSON envelope): a
    global Strategy/Budget/Pages Fetched/Total Chars/Truncated header
    followed by one Title/URL/From Index/Chars/Truncated section per page
    (body after a `---` line). Failed URLs get a URL/Status/Error section."""
    if not out.get("ok"):
        lines = [
            "Pages Fetched: 0",
            "Status: failed",
            f"Error: {out.get('error') or 'unknown error'}",
        ]
        tline = format_timing(out.get("timing"))
        if tline:
            lines.append(tline)
        sections = [
            "\n".join([
                f"URL: {p.get('url') or ''}",
                "Status: failed",
                f"Error: {p.get('error') or ''}",
            ])
            for p in out.get("pages") or []
        ]
        if not sections:
            return "\n".join(lines)
        return "\n\n".join(["\n".join(lines), "\n\n".join(sections)])

    lines = [f"Strategy: {out.get('strategy') or 'unknown'}"]
    if out.get("budget"):
        lines.append(f"Budget: {out['budget']}")
    lines += [
        f"Pages Fetched: {out.get('pages_fetched') or 0}",
        f"Total Chars: {out.get('total_chars') or 0}",
        f"Truncated: {'true' if out.get('truncated') else 'false'}",
    ]
    tline = format_timing(out.get("timing"))
    if tline:
        lines.append(tline)
    sections = []
    for p in out.get("pages") or []:
        if p.get("error"):
            sections.append("\n".join([
                f"URL: {p['url']}",
                "Status: failed",
                f"Error: {p['error']}",
            ]))
            continue
        sections.append("\n".join([
            f"Title: {p.get('title') or p['url']}",
            f"URL: {p['url']}",
            f"From Index: {'true' if p.get('from_index') else 'false'}",
            f"Chars: {p.get('chars') or len(p.get('content') or '')}",
            f"Truncated: {'true' if p.get('truncated') else 'false'}",
            "---",
            p.get("content") or "",
        ]))
    if not sections:
        return "\n".join(lines)
    return "\n\n".join(["\n".join(lines), "\n\n".join(sections)])


async def fetch_page(url: str, max_chars: int | None = None) -> dict:
    """Single-URL read: stored or crawled-on-demand, fetch_count bumped."""
    t_start = time.monotonic()

    def _timing(**extra: int) -> dict:
        """Timing dict: elapsed ms since the call started, plus any extras."""
        t: dict = {"total_ms": int((time.monotonic() - t_start) * 1000)}
        t.update(extra)
        return t

    if not _valid_url(url):
        return {"ok": False, "error": f"invalid or non-http(s) url: {url!r}", "url": url, "timing": _timing()}

    try:
        page = await _resolve_page(url)
    except Exception as e:
        log.warning("fetch_page failed for %s: %s", url, e)
        return {"ok": False, "error": str(e), "url": url, "timing": _timing()}

    await db.bump_fetch_count(url)

    truncated = False
    omitted = 0
    text = page["content"]
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        text = truncate_page(text, max_chars, "head")[0]
        truncated = True
        omitted = len(page["content"]) - len(text)
        text = text + "\n" + truncation_marker(omitted, "head")

    timing = _timing(index_ms=page.get("index_ms", 0))
    if page.get("crawl_ms"):
        timing["crawl_ms"] = page["crawl_ms"]

    return {
        "ok": True,
        "url": url,
        "title": page["title"],
        "markdown": text,
        "chars": len(text),
        "truncated": truncated,
        "from_index": page["from_index"],
        "timing": timing,
    }


async def fetch_pages(
    urls: list[str],
    max_chars: int | None = _OMITTED,  # type: ignore[assignment]
    per_page_chars: int | None = _OMITTED,  # type: ignore[assignment]
    strategy: str | None = None,
) -> dict:
    """Bulk read under a shared char budget with a swappable strategy."""
    s = get_settings()
    t_start = time.monotonic()

    def _timing(**extra: int) -> dict:
        """Timing dict: elapsed ms since the call started, plus any extras."""
        t: dict = {"total_ms": int((time.monotonic() - t_start) * 1000)}
        t.update(extra)
        return t

    # --- validate + dedupe (preserve first-seen order)
    clean, bad = _validate_urls(urls)

    if not clean:
        return {
            "ok": False,
            "error": "no valid urls provided",
            "pages": bad,
            "timing": _timing(),
        }

    strat = (strategy or s.FETCH_DEFAULT_STRATEGY).lower()
    if strat not in STRATEGIES:
        return {
            "ok": False,
            "error": f"unknown strategy {strat!r} (choose from {list(STRATEGIES)})",
            "pages": bad,
            "timing": _timing(),
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
    resolved, failed = await _resolve_all(clean)
    if not resolved:
        return {
            "ok": False,
            "error": "all urls failed to fetch",
            "pages": failed + bad,
            "timing": _timing(),
        }

    # bump fetch_count for every successfully fetched page
    for p in resolved:
        await db.bump_fetch_count(p["url"])

    # --- allocate the budget per strategy
    pages_out, total_chars, any_truncated = _allocate_pages(resolved, strat, budget, per_page)

    # Pages resolved in parallel, so each leg is the critical path (max), not
    # the sum. Legs are per-leg critical paths: when different pages dominate
    # different legs the sum of the legs can approach or slightly exceed the
    # total (each leg individually still does not).
    index_ms = max(p.get("index_ms", 0) for p in resolved)
    crawl_ms = max(p.get("crawl_ms", 0) for p in resolved)
    timing = _timing(index_ms=index_ms)
    if crawl_ms:
        timing["crawl_ms"] = crawl_ms

    return {
        "ok": True,
        "pages_fetched": len(resolved),
        "truncated": any_truncated,
        "total_chars": total_chars,
        "strategy": strat,
        "budget": budget,
        "pages": pages_out + failed + bad,
        "timing": timing,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_url(url: str) -> bool:
    """True when the URL is http(s) with a host."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _title_from_markdown(md: str) -> str | None:
    """First H1, else the first non-empty line (120 chars), else None."""
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    if m:
        return m.group(1).strip()
    for line in md.splitlines():
        line = line.strip()
        if line:
            return line[:TITLE_MAX_LEN]
    return None


async def _resolve_page(url: str) -> dict:
    """Content for one URL: from index when present, else crawl on demand.

    Carries `index_ms` (the Postgres lookup) and, when crawled, `crawl_ms`
    (the native-crawler round-trip + store) so callers can report the timing split.
    """
    t_index = time.monotonic()
    page = await db.page_get(url)
    index_ms = int((time.monotonic() - t_index) * 1000)
    if page and not page.get("disabled") and page.get("fit_markdown"):
        return {
            "url": url,
            "title": page.get("title") or _title_from_markdown(page["fit_markdown"]) or url,
            "content": page["fit_markdown"],
            "from_index": True,
            "fetch_count": page.get("fetch_count") or 0,
            "index_ms": index_ms,
            "crawl_ms": 0,
        }

    # crawl on demand (in-flight-deduped inside crawl_url)
    t_crawl = time.monotonic()
    r = await crawl_url(url, trigger="fetch")
    page = await db.page_get(url)
    crawl_ms = int((time.monotonic() - t_crawl) * 1000)
    md = (page or {}).get("fit_markdown") or ""
    if not md:
        raise RuntimeError(f"crawl succeeded but no content stored for {url}")
    return {
        "url": url,
        "title": (page or {}).get("title") or _title_from_markdown(md) or url,
        "content": md,
        "from_index": False,
        "fetch_count": (page or {}).get("fetch_count") or 0,
        "index_ms": index_ms,
        "crawl_ms": crawl_ms,
    }


def _validate_urls(urls: list[str]) -> tuple[list[str], list[dict]]:
    """Validate + dedupe (preserve first-seen order). Returns (clean, bad)."""
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
    return clean, bad


async def _resolve_all(urls: list[str]) -> tuple[list[dict], list[dict]]:
    """Resolve every URL in parallel (in-flight-deduped).
    Returns (resolved, failed)."""
    resolved: list[dict] = []
    failed: list[dict] = []

    async def _one(u: str) -> None:
        """Resolve one URL, routing it to resolved or failed."""
        try:
            resolved.append(await _resolve_page(u))
        except Exception as e:
            log.warning("fetch_pages: %s failed: %s", u, e)
            failed.append({"url": u, "error": str(e)[:ERROR_MAX_LEN]})

    await asyncio.gather(*(_one(u) for u in urls))
    return resolved, failed


def _allocate_pages(
    resolved: list[dict],
    strat: str,
    budget: int | None,
    per_page: int | None,
) -> tuple[list[dict], int, bool]:
    """Allocate the shared char budget per strategy and assemble the page
    dicts. Returns (pages_out, total_chars, any_truncated)."""
    lens = [len(p["content"]) for p in resolved]
    weights = [p["fetch_count"] for p in resolved]
    budgets = allocate_budgets(strat, lens, weights, budget, per_page)

    pages_out: list[dict] = []
    total_chars = 0
    any_truncated = False

    for p, chars in zip(resolved, budgets):
        text, truncated = truncate_page(p["content"], chars, strat)
        omitted = len(p["content"]) - len(text)
        if truncated:
            text = text + "\n" + truncation_marker(omitted, strat)
            any_truncated = True
        total_chars += len(text)
        pages_out.append({
            "url": p["url"],
            "title": p["title"],
            "content": text,
            "chars": len(text),
            "truncated": truncated,
            "omitted": omitted if truncated else 0,
            "from_index": p["from_index"],
        })
    return pages_out, total_chars, any_truncated
