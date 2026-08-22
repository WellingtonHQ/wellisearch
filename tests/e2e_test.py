r"""E2E pass (BLUEPRINT section 14) against the running server on 127.0.0.1:8780.

Run: .venv/Scripts/python.exe tests/e2e_test.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

import httpx

BASE = "http://127.0.0.1:8780"
KEY = "BSAm40FgxtTrX_hDRzpJ0cDg7Oki4Qy"
H = {"X-API-Key": KEY}
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def first_url(markdown: str) -> str | None:
    m = re.search(r"URL: (\S+)", markdown)
    return m.group(1) if m else None


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, headers=H, timeout=120) as c:
        # 1. health ----------------------------------------------------------------
        r = await c.get("/health")
        j = r.json()
        check("health: db + crawl4ai ok", r.status_code == 200 and j["database"] == "ok" and j["crawl4ai"] == "ok", json.dumps(j)[:160])

        # 2. auth (separate keyless clients so client-level headers don't leak) ----
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as open_c:
            r = await open_c.get("/api/stats")
            check("REST auth: missing key -> 401", r.status_code == 401, str(r.status_code))
        async with httpx.AsyncClient(base_url=BASE, headers={"Authorization": f"Bearer {KEY}"}, timeout=30) as b_c:
            r = await b_c.get("/api/stats")
            check("REST auth: Bearer key -> 200", r.status_code == 200, str(r.status_code))

        # 3. gateway search (local index may be empty -> provider serves) -------------
        r = await c.get("/api/search", params={"query": "fastapi mcp server", "k": "5"})
        j = r.json()
        check("search: 200 + count>=3 + degraded=false", r.status_code == 200 and j.get("count", 0) >= 3 and j.get("degraded") is False,
              f"count={j.get('count')} degraded={j.get('degraded')} src={j.get('source')}")
        check("search: Markdown contract Title/URL/Snippet",
              all(k in j.get("results", "") for k in ("Title:", "URL:", "Snippet:")), j.get("results", "")[:120].replace("\n", " | "))

        # 4. fetch a page (crawl + index) ---------------------------------------------
        url = first_url(j.get("results", "")) or "https://python.langchain.com/docs/introduction/"
        r = await c.post("/api/fetch", json={"url": url})
        j = r.json()
        check(f"fetch: ok + markdown ({url})", r.status_code == 200 and j.get("ok") is True and len(j.get("markdown", "")) > 200,
              f"chars={j.get('chars')} from_index={j.get('from_index')}")

        # 5. page indexed --------------------------------------------------------------
        r = await c.get("/api/pages")
        rows = (r.json() or {}).get("pages", []) if r.status_code == 200 else []
        row = next((p for p in rows if p.get("url") == url), None)
        check("page row exists (indexed)", row is not None and (row.get("crawl_count") or 0) >= 1,
              f"crawl_count={row.get('crawl_count') if row else 'NO ROW'} status={row.get('last_status') if row else '-'}")

        # 6. local search now hits the index (zero provider cost) ----------------------
        r = await c.get("/api/search", params={"query": url.split("/")[2] + " introduction"})
        j = r.json()
        check("local search: 200 + degraded=false", r.status_code == 200 and j.get("degraded") is False, f"src={j.get('source')}")

        # 7. fetch-bulk with truncation --------------------------------------------------
        r = await c.post("/api/fetch-bulk", json={
            "urls": ["https://python.langchain.com/docs/introduction/",
                     "https://python.langchain.com/docs/get_started/quickstart/"],
            "max_chars": 3000, "strategy": "even"})
        j = r.json()
        check("fetch-bulk: ok + 2 pages fetched", r.status_code == 200 and j.get("ok") is True and j.get("pages_fetched") == 2,
              f"pages_fetched={j.get('pages_fetched')}")
        check("fetch-bulk: shared budget respected", (j.get("total_chars") or 0) <= 3400, f"total_chars={j.get('total_chars')}")
        check("fetch-bulk: truncation marker present", j.get("truncated") is True and "[truncated" in j.get("markdown", ""), "marker" if "[truncated" in j.get("markdown", "") else "no marker")

        # 8. provider failover: disable tavily + local index -> brave serves ------------
        # (local-first is correct behavior, so the index is disabled to force the gateway)
        r = await c.patch("/api/providers/tavily", json={"enabled": False})
        check("provider toggle: tavily disabled", r.status_code == 200 and r.json().get("enabled") is False, r.text[:120])

        from urllib.parse import quote

        async def all_page_urls() -> list[str]:
            rr = await c.get("/api/pages", params={"limit": "100"})
            return [p["url"] for p in rr.json().get("pages", [])]

        async def set_disabled(urls: list[str], disabled: bool) -> None:
            for u in urls:
                await c.patch(f"/api/pages/{quote(u, safe='')}", json={"disabled": disabled})

        # local-first is correct, so disable the index to force the gateway.
        # the background worker may index pages concurrently (from enqueued
        # searches), so re-disable and retry if a local hit still wins.
        j: dict = {}
        for _attempt in range(3):
            await set_disabled(await all_page_urls(), True)
            r = await c.get("/api/search", params={"query": "chess grandmaster tournament live results", "k": "5"})
            j = r.json()
            if r.status_code == 200 and j.get("source") != "local":
                break
        check("failover: search 200 via brave", r.status_code == 200 and j.get("count", 0) >= 3 and j.get("source") == "brave",
              f"src={j.get('source')} count={j.get('count')}")

        # restore state
        await set_disabled(await all_page_urls(), False)
        r = await c.patch("/api/providers/tavily", json={"enabled": True})
        check("provider toggle: tavily re-enabled", r.status_code == 200 and r.json().get("enabled") is True, r.text[:120])

        # 9. stats / logs ----------------------------------------------------------------
        r = await c.get("/api/stats")
        j = r.json()
        check("stats: index pages>=1, 24h searches>=1",
              (j.get("index", {}).get("pages") or 0) >= 1 and (j.get("search_trends", {}).get("24h", {}).get("total") or 0) >= 1,
              json.dumps(j)[:160])
        # trigger a fresh crawl so the log check doesn't depend on pre-existing history
        r = await c.post("/api/refresh", json={"url": url}, timeout=120)
        check("refresh: 200 (crawl executed)", r.status_code == 200 and r.json().get("ok") is True, r.text[:120])
        r = await c.get("/api/logs/crawls")
        check("logs/crawls: rows present", r.status_code == 200 and len(r.json().get("crawls", [])) >= 1, f"n={len(r.json().get('crawls', []))}")
        r = await c.get("/api/logs/searches")
        check("logs/searches: rows present", r.status_code == 200 and len(r.json().get("searches", [])) >= 1, f"n={len(r.json().get('searches', []))}")

        # 10. dashboard HTML ----------------------------------------------------------------
        r = await c.get("/")
        check("dashboard: 200 + html", r.status_code == 200 and "<html" in r.text.lower(), f"len={len(r.text)}")

        # 11. MCP over SSE --------------------------------------------------------------------
        await mcp_pass()

    print(f"\n===== E2E RESULT: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


async def mcp_pass() -> None:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    url = f"{BASE}/mcp/sse"
    try:
        async with sse_client(url, headers={"X-API-Key": KEY}) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check("mcp: initialize (server name)", "wellisearch" in (init.server_info.name if init.server_info else ""), str(init.server_info))
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                expect = {"fetch_page", "fetch_pages", "index_stats", "refresh_page", "seed_url", "search_web"}
                check("mcp: exactly 6 tools", names == expect, str(sorted(names)))

                res = await session.call_tool("index_stats", {})
                j = json.loads(res.content[0].text if res.content else "{}")
                check("mcp: index_stats shape", isinstance(j, dict) and "index" in j and "quota_this_month" in j, json.dumps(j)[:150])

                res = await session.call_tool("search_web", {"query": "fastapi", "num_results": 3})
                j = json.loads(res.content[0].text if res.content else "{}")
                check("mcp: search_web Markdown + metadata", isinstance(j, dict) and "URL:" in j.get("results", "") and j.get("count", 0) >= 1,
                      f"count={j.get('count')} src={j.get('source')}")
    except Exception as e:
        check("mcp: SSE session", False, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
