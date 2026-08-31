r"""E2E pass (BLUEPRINT section 14) against the running server on 127.0.0.1:8780.

One test function per API surface (health, auth, search, fetch, pages,
providers, stats, logs, window, dashboard, format=json, mcp/http);
`main()` only orders them, since later steps reuse state from earlier ones.

Run: .venv/Scripts/python.exe tests/e2e_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from mcp import ClientSession

BASE = "http://127.0.0.1:8780"
DEFAULT_URL = "https://python.langchain.com/docs/introduction/"


def check(
    name: str,
    cond: bool,
    detail: str = "",
) -> None:
    """Record one assertion: bump PASS/FAIL and print the outcome."""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def first_url(markdown: str) -> str | None:
    """The first `URL: <url>` value in the Markdown, or None."""
    m = re.search(r"URL: (\S+)", markdown)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def test_health(c: httpx.AsyncClient) -> None:
    """GET /health: database + crawl4ai both ok."""
    r = await c.get("/health")
    j = r.json()
    check(
        "health: db + crawl4ai ok",
        r.status_code == 200 and j["database"] == "ok" and j["crawl4ai"] == "ok",
        json.dumps(j)[:160]
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_auth() -> None:
    """REST auth (separate keyless clients so client-level headers don't leak)."""
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as open_c:
        r = await open_c.get("/api/stats")
        check("REST auth: missing key -> 401", r.status_code == 401, str(r.status_code))
    async with httpx.AsyncClient(
        base_url=BASE, headers={"Authorization": f"Bearer {KEY}"}, timeout=30
    ) as b_c:
        r = await b_c.get("/api/stats")
        check("REST auth: Bearer key -> 200", r.status_code == 200, str(r.status_code))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def test_search_gateway(c: httpx.AsyncClient) -> str:
    """GET /api/search (gateway): Markdown contract, >=2 results, degraded=false.

    Returns the first result URL (or DEFAULT_URL) for the downstream
    fetch/index checks.
    """
    r = await c.get("/api/search", params={"query": "fastapi mcp server", "k": "5"})
    md = r.text
    # >=2 results (not 3): the coverage gate may legitimately
    # drop weak local hits, so a strong query can return 2. Zero is the real failure.
    check(
        "search: 200 + >=2 results + degraded=false",
        r.status_code == 200 and len(re.findall(r"^URL: ", md, re.M)) >= 2 and "Degraded: false" in md,
        md[:120].replace("\n", " | ")
    )
    check(
        "search: Markdown contract header + Title/URL/Snippet",
        all(k in md for k in ("Source:", "Title:", "URL:", "Snippet:")),
        md[:120].replace("\n", " | ")
    )
    check(
        "search: Time line present with index: split",
        re.search(r"^Time: \d+ ms \(index: \d+ ms", md, re.M) is not None,
        (re.search(r"^Time: .*", md, re.M) or [None, "MISSING"])[0]
    )
    return first_url(md) or DEFAULT_URL


async def test_local_search(c: httpx.AsyncClient, url: str) -> None:
    """GET /api/search (local index hit): zero provider cost, real Title != URL."""
    r = await c.get("/api/search", params={"query": url.split("/")[2] + " introduction"})
    md = r.text
    check(
        "local search: 200 + degraded=false",
        r.status_code == 200 and "Degraded: false" in md and "URL:" in md,
        md[:120].replace("\n", " | "),
    )
    # the locally-indexed result must carry a real Title, not the URL —
    # the original bug had every local `Title:` line equal to its `URL:`
    title_url = re.findall(r"^Title: (.+)$\n^URL: (.+)$", md, re.M)
    check(
        "local search: at least one Title != URL",
        bool(title_url) and any(t.strip() != u.strip() for t, u in title_url),
        f"pairs={title_url[:2]}"
    )


async def test_search_mode_provider(c: httpx.AsyncClient) -> None:
    """GET /api/search search_mode=provider: bypass the index, force a live provider.

    A query that normally hits local must now come from a provider.
    """
    params = {"query": "fastapi mcp server", "k": "5", "search_mode": "provider", "format": "json"}
    r = await c.get("/api/search", params=params)
    j = r.json() if r.status_code in (200, 502) else {}
    t = j.get("timing", {}) if isinstance(j, dict) else {}
    check(
        "search_mode=provider: 200 + non-local source",
        r.status_code == 200 and j.get("source") not in (None, "local", "error"),
        f"source={j.get('source')} status={r.status_code}"
    )
    check(
        "search_mode=provider: index_ms absent (index leg skipped) + provider_ms present",
        "index_ms" not in t and "provider_ms" in t,
        json.dumps(t)
    )
    check(
        "search_mode=provider: results returned",
        isinstance(j.get("results"), list) and len(j["results"]) >= 1,
        f"n={len(j.get('results', [])) if isinstance(j.get('results'), list) else '-'}"
    )


async def test_search_mode_local(c: httpx.AsyncClient, url: str) -> None:
    """GET /api/search search_mode=local: index only (a query the index has)."""
    params = {
        "query": url.split("/")[2] + " introduction",
        "k": "5",
        "search_mode": "local",
        "format": "json",
    }
    r = await c.get("/api/search", params=params)
    j = r.json() if r.status_code in (200, 502) else {}
    t = j.get("timing", {}) if isinstance(j, dict) else {}
    check(
        "search_mode=local: 200 + local source",
        r.status_code == 200 and j.get("source") == "local",
        f"source={j.get('source')} status={r.status_code}"
    )
    check(
        "search_mode=local: index_ms present, no provider_ms",
        "index_ms" in t and "provider_ms" not in t,
        json.dumps(t)
    )
    check(
        "search_mode=local: results returned",
        isinstance(j.get("results"), list) and len(j["results"]) >= 1,
        f"n={len(j.get('results', [])) if isinstance(j.get('results'), list) else '-'}"
    )


async def test_search_mode_invalid(c: httpx.AsyncClient) -> None:
    """GET /api/search: invalid search_mode -> 400."""
    r = await c.get("/api/search", params={"query": "anything", "search_mode": "bogus"})
    check("invalid search_mode: 400", r.status_code == 400, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def test_fetch(c: httpx.AsyncClient, url: str) -> None:
    """POST /api/fetch: crawl + index, Markdown header + body."""
    r = await c.post("/api/fetch", json={"url": url})
    md2 = r.text
    check(
        f"fetch: 200 + Markdown header + body ({url})",
        r.status_code == 200
        and all(k in md2 for k in ("Title:", "URL:", "From Index:", "Chars:", "Truncated:"))
        and len(md2) > 200,
        md2[:120].replace("\n", " | ")
    )
    check(
        "fetch: Time line present with index: split",
        re.search(r"^Time: \d+ ms \(index: \d+ ms", md2, re.M) is not None,
        (re.search(r"^Time: .*", md2, re.M) or [None, "MISSING"])[0]
    )


async def test_page_indexed(c: httpx.AsyncClient, url: str) -> None:
    """GET /api/pages: the fetched URL now has an indexed row (crawl_count >= 1)."""
    r = await c.get("/api/pages")
    rows = (r.json() or {}).get("pages", []) if r.status_code == 200 else []
    row = next((p for p in rows if p.get("url") == url), None)
    check(
        "page row exists (indexed)",
        row is not None and (row.get("crawl_count") or 0) >= 1,
        f"crawl_count={row.get('crawl_count') if row else 'NO ROW'} "
        f"status={row.get('last_status') if row else '-'}",
    )


async def test_fetch_bulk(c: httpx.AsyncClient) -> None:
    """POST /api/fetch-bulk: global header, one section per page, budget respected."""
    r = await c.post(
        "/api/fetch-bulk",
        json={
            "urls": ["https://python.langchain.com/docs/introduction/",
                     "https://python.langchain.com/docs/get_started/quickstart/"],
            "max_chars": 3000, "strategy": "even"}
    )
    md2 = r.text
    total_m = re.search(r"Total Chars:\s*(\d+)", md2)
    total = int(total_m.group(1)) if total_m else -1
    check(
        "fetch-bulk: 200 + Markdown header + 2 sections",
        r.status_code == 200
        and all(k in md2 for k in ("Strategy:", "Pages Fetched:", "Total Chars:", "Truncated:"))
        and "Pages Fetched: 2" in md2
        and len(re.findall(r"^Title: ", md2, re.M)) == 2,
        md2[:120].replace("\n", " | ")
    )
    check("fetch-bulk: shared budget respected", total >= 0 and total <= 3400, f"total_chars={total}")
    check(
        "fetch-bulk: truncation marker present",
        "Truncated: true" in md2 and "[truncated" in md2,
        "marker" if "[truncated" in md2 else "no marker"
    )
    check(
        "fetch-bulk: Time line present with index: split",
        re.search(r"^Time: \d+ ms \(index: \d+ ms", md2, re.M) is not None,
        (re.search(r"^Time: .*", md2, re.M) or [None, "MISSING"])[0]
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def test_provider_failover(c: httpx.AsyncClient) -> None:
    """PATCH /api/providers/tavily + gateway failover: disable tavily + local index -> brave serves."""
    # (local-first is correct behavior, so the index is disabled to force the gateway)
    r = await c.patch("/api/providers/tavily", json={"enabled": False})
    check(
        "provider toggle: tavily disabled",
        r.status_code == 200 and r.json().get("enabled") is False,
        r.text[:120],
    )

    # local-first is correct, so force the gateway two ways: disable the
    # most-read pages, AND use a query the index cannot cover ("quixotic
    # zzyzx" tops out at 0.5 coverage < LOCAL_MIN_COVERAGE — verified
    # 2026-08-25; any 3+ real-word query has a full-coverage page in a
    # 25k-page corpus). The worker may index concurrently, so retry if a
    # local hit still wins.
    for _attempt in range(3):
        await _set_pages_disabled(c, await _all_page_urls(c), True)
        r = await c.get("/api/search", params={"query": "quixotic zzyzx", "k": "5"})
        if r.status_code == 200 and "Source: local" not in r.text:
            break
    check(
        "failover: search 200 via brave",
        r.status_code == 200
        and len(re.findall(r"^URL: ", r.text, re.M)) >= 3
        and "Source: brave" in r.text,
        r.text[:120].replace("\n", " | "),
    )

    # restore state
    await _set_pages_disabled(c, await _all_page_urls(c), False)
    r = await c.patch("/api/providers/tavily", json={"enabled": True})
    check(
        "provider toggle: tavily re-enabled",
        r.status_code == 200 and r.json().get("enabled") is True,
        r.text[:120],
    )


async def test_provider_order(c: httpx.AsyncClient) -> None:
    """GET/PUT /api/providers + /api/providers/order: runtime reorder + validation + reset."""
    r = await c.get("/api/providers")
    j = r.json()
    base_order = j.get("order", [])
    check(
        "order: GET /api/providers exposes order + order_source",
        r.status_code == 200 and isinstance(base_order, list) and len(base_order) >= 1
        and j.get("order_source") in ("env", "runtime")
        and all(isinstance(p.get("order"), int) for p in j.get("providers", [])),
        json.dumps({"order": base_order, "source": j.get("order_source")})[:160]
    )

    # invalid: duplicate provider -> 400
    if base_order:
        dup = [base_order[0], base_order[0]] + base_order[1:]
        r = await c.put("/api/providers/order", json={"order": dup})
        check(
            "order: duplicate provider -> 400",
            r.status_code == 400,
            f"status={r.status_code} {r.text[:100]}",
        )
    # invalid: missing a provider (not a permutation) -> 400
    if len(base_order) >= 2:
        r = await c.put("/api/providers/order", json={"order": base_order[:-1]})
        check(
            "order: non-permutation (missing provider) -> 400",
            r.status_code == 400,
            f"status={r.status_code} {r.text[:100]}"
        )
    if len(base_order) < 2:
        return

    # reorder: reverse the base order and confirm it sticks
    rev = list(reversed(base_order))
    r = await c.put("/api/providers/order", json={"order": rev})
    check(
        "order: PUT reorder -> 200 + runtime source",
        r.status_code == 200 and r.json().get("order") == rev and r.json().get("order_source") == "runtime",
        r.text[:120]
    )
    r = await c.get("/api/providers")
    j = r.json()
    check(
        "order: GET reflects runtime order (reversed)",
        j.get("order") == rev and j.get("order_source") == "runtime"
        and [p["name"] for p in j.get("providers", [])] == rev
        and [p["order"] for p in j.get("providers", [])] == list(range(len(rev))),
        json.dumps(j.get("order"))[:160]
    )
    # stats surface the same order
    r = await c.get("/api/stats")
    gw = r.json().get("gateway", {})
    check(
        "order: /api/stats gateway.provider_order matches",
        r.status_code == 200 and gw.get("provider_order") == rev and gw.get("order_source") == "runtime",
        json.dumps(gw)[:160]
    )

    # reset -> back to env default order
    r = await c.put("/api/providers/order", json={"order": None})
    check(
        "order: PUT order=null resets -> env source",
        r.status_code == 200
        and r.json().get("order_source") == "env"
        and r.json().get("order") == base_order,
        r.text[:120],
    )
    r = await c.get("/api/providers")
    check(
        "order: GET back to env default after reset",
        r.json().get("order") == base_order and r.json().get("order_source") == "env",
        json.dumps(r.json().get("order"))[:160]
    )


# ---------------------------------------------------------------------------
# Stats + Logs
# ---------------------------------------------------------------------------

async def test_stats_logs(c: httpx.AsyncClient, url: str) -> None:
    """GET /api/stats, POST /api/refresh, GET /api/logs/crawls + /api/logs/searches."""
    r = await c.get("/api/stats")
    j = r.json()
    pages = j.get("index", {}).get("pages") or 0
    searches_24h = j.get("search_trends", {}).get("24h", {}).get("total") or 0
    check(
        "stats: index pages>=1, 24h searches>=1",
        pages >= 1 and searches_24h >= 1,
        json.dumps(j)[:160]
    )
    # trigger a fresh crawl so the log check doesn't depend on pre-existing history
    r = await c.post("/api/refresh", json={"url": url}, timeout=120)
    check("refresh: 200 (crawl executed)", r.status_code == 200 and r.json().get("ok") is True, r.text[:120])
    r = await c.get("/api/logs/crawls")
    crawls = r.json().get("crawls", [])
    check("logs/crawls: rows present", r.status_code == 200 and len(crawls) >= 1, f"n={len(crawls)}")
    r = await c.get("/api/logs/searches")
    searches = r.json().get("searches", [])
    check("logs/searches: rows present", r.status_code == 200 and len(searches) >= 1, f"n={len(searches)}")


async def test_window_logs(c: httpx.AsyncClient) -> None:
    """GET /api/window + GET /api/logs: window shape, clamping, merged log stream."""
    r = await c.get("/api/window", params={"secs": "86400"})
    j = r.json()
    check(
        "window: 200 + shape + searches>=1",
        r.status_code == 200 and j.get("secs") == 86400
        and (j.get("searches", {}).get("total") or 0) >= 1
        and "by_status" in j.get("crawls", {})
        and "local_rate" in j.get("searches", {}),
        json.dumps(j)[:160]
    )
    r = await c.get("/api/window", params={"secs": "1"})
    j = r.json()
    check(
        "window: secs clamped to 600 floor",
        r.status_code == 200 and j.get("secs") == 600,
        f"secs={j.get('secs')}",
    )
    r = await c.get("/api/logs", params={"secs": "86400", "limit": "50"})
    j = r.json()
    logs = j.get("logs", [])
    check(
        "logs: 200 + rows with ts/kind/message/info",
        r.status_code == 200 and len(logs) >= 1
        and all(k in logs[0] for k in ("ts", "kind", "message", "info")),
        f"n={len(logs)} first={json.dumps(logs[0])[:120] if logs else '-'}"
    )
    r10 = await c.get("/api/logs", params={"secs": "600"})
    j10 = r10.json()
    r24 = await c.get("/api/logs", params={"secs": "86400"})
    j24 = r24.json()
    check(
        "logs: 10m window is a subset of 24h window",
        r10.status_code == 200 and (j10.get("total") or 0) <= (j24.get("total") or 0),
        f"10m={j10.get('total')} 24h={j24.get('total')}"
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def test_dashboard(c: httpx.AsyncClient) -> None:
    """GET /: dashboard HTML served."""
    r = await c.get("/")
    check("dashboard: 200 + html", r.status_code == 200 and "<html" in r.text.lower(), f"len={len(r.text)}")


# ---------------------------------------------------------------------------
# Format=JSON
# ---------------------------------------------------------------------------

async def test_format_json(c: httpx.AsyncClient, url: str) -> None:
    """format=json on /api/search + /api/fetch + /api/fetch-bulk: envelope + precedence."""
    # search: explicit format=json -> JSON envelope
    params = {"query": "fastapi mcp server", "num_results": 5, "format": "json"}
    r = await c.get("/api/search", params=params)
    j = r.json()
    check(
        "search json: 200 + content-type json",
        r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"),
        r.headers.get("content-type", "")
    )
    check(
        "search json: envelope keys",
        isinstance(j, dict) and all(k in j for k in ("source", "degraded", "count", "results")),
        str(sorted(j.keys())) if isinstance(j, dict) else type(j).__name__
    )
    check(
        "search json: results list with url/title/snippet",
        isinstance(j.get("results"), list) and len(j["results"]) >= 1
        and all(k in j["results"][0] for k in ("url", "title", "snippet")),
        json.dumps((j.get("results") or [{}])[0])[:120]
    )
    check(
        "search json: timing object with total_ms + index_ms",
        isinstance(j.get("timing"), dict) and "total_ms" in j["timing"] and "index_ms" in j["timing"],
        json.dumps(j.get("timing"))
    )

    # search: Accept header only (no format param) -> JSON
    r = await c.get(
        "/api/search",
        params={"query": "fastapi mcp server"},
        headers={"Accept": "application/json"},
    )
    check(
        "search json via Accept header",
        r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
        and "results" in r.json(),
        r.headers.get("content-type", "")
    )

    # precedence: format=markdown + Accept: application/json -> param wins
    r = await c.get(
        "/api/search",
        params={"query": "fastapi mcp server", "format": "markdown"},
        headers={"Accept": "application/json"}
    )
    check(
        "search precedence: format param wins over Accept",
        r.status_code == 200
        and r.headers.get("content-type", "").startswith("text/markdown")
        and "Source:" in r.text,
        r.headers.get("content-type", ""),
    )

    # invalid format -> 400
    r = await c.get("/api/search", params={"query": "fastapi", "format": "yaml"})
    check("search invalid format -> 400", r.status_code == 400, r.text[:120])

    # fetch: format=json -> JSON envelope
    r = await c.post("/api/fetch", json={"url": url, "format": "json"})
    j = r.json()
    check(
        "fetch json: 200 + content-type json + envelope keys",
        r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
        and all(k in j for k in ("ok", "url", "title", "markdown", "chars", "truncated", "from_index")),
        r.headers.get("content-type", "") + " " + json.dumps(j)[:100]
    )
    check(
        "fetch json: timing object with total_ms + index_ms",
        isinstance(j.get("timing"), dict) and "total_ms" in j["timing"] and "index_ms" in j["timing"],
        json.dumps(j.get("timing"))
    )

    # fetch-bulk: format=json -> JSON envelope
    r = await c.post(
        "/api/fetch-bulk",
        json={
            "urls": ["https://python.langchain.com/docs/introduction/",
                     "https://python.langchain.com/docs/get_started/quickstart/"],
            "max_chars": 3000, "strategy": "even", "format": "json"}
    )
    j = r.json()
    check(
        "fetch-bulk json: 200 + content-type json + envelope keys",
        r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
        and all(k in j for k in ("ok", "pages_fetched", "total_chars", "strategy", "pages")),
        r.headers.get("content-type", "") + " " + json.dumps(j)[:100]
    )
    check(
        "fetch-bulk json: pages list with content/chars",
        isinstance(j.get("pages"), list) and len(j["pages"]) >= 1
        and all(k in j["pages"][0] for k in ("url", "title", "content", "chars", "truncated")),
        json.dumps((j.get("pages") or [{}])[0])[:120]
    )
    check(
        "fetch-bulk json: timing object with total_ms + index_ms",
        isinstance(j.get("timing"), dict) and "total_ms" in j["timing"] and "index_ms" in j["timing"],
        json.dumps(j.get("timing"))
    )


# ---------------------------------------------------------------------------
# MCP/HTTP
# ---------------------------------------------------------------------------

async def mcp_http_pass() -> None:
    """Section 11: MCP over stateless Streamable HTTP (auth, 404s, session)."""
    try:
        await _mcp_http_auth_checks()
        await _mcp_http_session_checks()
    except Exception as e:
        check("mcp/http: stateless streamable session", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the whole E2E pass, in order (later steps reuse earlier state)."""
    async with httpx.AsyncClient(base_url=BASE, headers=H, timeout=120) as c:
        await test_health(c)
        await test_auth()
        url = await test_search_gateway(c)
        await test_fetch(c, url)
        await test_page_indexed(c, url)
        await test_local_search(c, url)
        await test_search_mode_provider(c)
        await test_search_mode_local(c, url)
        await test_search_mode_invalid(c)
        await test_fetch_bulk(c)
        await test_provider_failover(c)
        await test_provider_order(c)
        await test_stats_logs(c, url)
        await test_window_logs(c)
        await test_dashboard(c)
        await test_format_json(c, url)
        await mcp_http_pass()

    print(f"\n===== E2E RESULT: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_api_key() -> str:
    """The API key from the environment or the .env file (exit if absent)."""
    key = os.environ.get("WELLISEARCH_API_KEY")
    if key:
        return key
    env_file = Path(__file__).resolve().parent.parent / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("WELLISEARCH_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("WELLISEARCH_API_KEY not found in environment or .env")


KEY = _load_api_key()
H = {"X-API-Key": KEY}
PASS, FAIL = 0, 0


async def _all_page_urls(c: httpx.AsyncClient) -> list[str]:
    """Every indexed page URL (limit 100)."""
    rr = await c.get("/api/pages", params={"limit": "100"})
    return [p["url"] for p in rr.json().get("pages", [])]


async def _set_pages_disabled(
    c: httpx.AsyncClient,
    urls: list[str],
    disabled: bool,
) -> None:
    """PATCH each page's disabled flag (forces/undoes gateway failover)."""
    for u in urls:
        await c.patch(f"/api/pages/{quote(u, safe='')}", json={"disabled": disabled})


async def _mcp_http_auth_checks() -> None:
    """Keyless requests must 401; the removed SSE endpoints must 404."""
    # auth: keyless POST must 401 (middleware's startswith("/mcp") prefix)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as open_c:
        r = await open_c.post(
            f"{BASE}/mcp/http",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        check("mcp/http auth: missing key -> 401", r.status_code == 401, str(r.status_code))

    # the SSE transport is gone: authenticated requests must 404
    # (auth middleware passes, routing finds no route)
    async with httpx.AsyncClient(base_url=BASE, headers={"X-API-Key": KEY}, timeout=30) as gone_c:
        r = await gone_c.get("/mcp/sse", headers={"Accept": "text/event-stream"})
        check("mcp/sse: removed -> 404", r.status_code == 404, str(r.status_code))
        r = await gone_c.post("/mcp/messages/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        check("mcp/messages/: removed -> 404", r.status_code == 404, str(r.status_code))


async def _mcp_http_session_checks() -> None:
    """Open one Streamable-HTTP session; drive the handshake + tool checks."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # the SDK's streamable client takes its own httpx2 client (for headers)
    import httpx2

    async with httpx2.AsyncClient(headers={"X-API-Key": KEY}) as http:
        async with streamable_http_client(f"{BASE}/mcp/http", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await _mcp_http_handshake_checks(session)
                await _mcp_http_tool_call_checks(session)


async def _mcp_http_handshake_checks(session: ClientSession) -> None:
    """initialize + tools/list: the session contract."""
    init = await session.initialize()
    check(
        "mcp/http: initialize (server name)",
        "wellisearch" in (init.server_info.name if init.server_info else ""),
        str(init.server_info)
    )

    tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    expect = {"fetch_page", "fetch_pages", "index_stats", "refresh_page", "seed_url", "search_web"}
    check("mcp/http: exactly 6 tools", names == expect, str(sorted(names)))


async def _mcp_http_tool_call_checks(session: ClientSession) -> None:
    """index_stats, search_web, then a 2nd request to prove statelessness."""
    res = await session.call_tool("index_stats", {})
    j = json.loads(res.content[0].text if res.content else "{}")
    check(
        "mcp/http: index_stats shape",
        isinstance(j, dict) and "index" in j and "quota_this_month" in j,
        json.dumps(j)[:150]
    )

    res = await session.call_tool("search_web", {"query": "fastapi", "num_results": 3})
    md = res.content[0].text if res.content else ""
    check(
        "mcp/http: search_web Markdown + header",
        "Source:" in md and "Title:" in md and "URL:" in md and "Snippet:" in md
        and len(re.findall(r"^URL: ", md, re.M)) >= 1,
        md[:120].replace("\n", " | ")
    )

    # stateless: a second request must work in the same
    # "session" (each POST gets a fresh transport server-side)
    res = await session.call_tool(
        "fetch_page", {"url": "https://python.langchain.com/docs/introduction/"})
    md = res.content[0].text if res.content else ""
    check(
        "mcp/http: fetch_page Markdown + header (2nd stateless request)",
        all(k in md for k in ("Title:", "URL:", "From Index:", "Chars:", "Truncated:"))
        and len(md) > 200,
        md[:120].replace("\n", " | ")
    )


if __name__ == "__main__":
    asyncio.run(main())
