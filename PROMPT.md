# wellisearch — opencode kickoff prompt

**How to use:** paste everything between the rules below into opencode, run from the stack root (the folder that will contain `infra/` and `wellisearch/`). The plan note **"2026-08-20-wellisearch — plan & layout (handoff for opencode)"** is the single source of truth; where this prompt and the plan disagree, the plan wins.

* * *

## Prompt

You are building **wellisearch** — a self-hosted search gateway + web-index service that powers an Open WebUI (OWUI) agent's web search and page reading. The full approved design lives in the plan note (sections §1–§16); references like §7 below point into it. Your job is to implement it end to end, exactly as specified, as real working code.

### Ground rules

1.  **The plan note is canonical.** Read it fully before writing any code. It defines the tool surface, data model, build order, and acceptance criteria you must hit.
    
2.  **§3 "Stack decisions (locked)" are fixed** — Python 3.12 + FastAPI + MCP SDK; Postgres 18 + pgvector in a **shared container at** `infra/postgres/` **(outside the app repo)**; fastembed `all-MiniLM-L6-v2` (384-dim, one `EMBED_MODEL` constant everywhere); Crawl4AI as the **only** crawling path; provider gateway `tavily → brave → searxng (optional)` with failover + monthly quota ledger; **async indexing** (search returns immediately, a debounced worker tick drains the crawl queue, read tools crawl on demand); 2 containers total. Do not re-litigate or substitute any of these.
    
3.  **The MCP tool surface is exactly §7** — `search_web`, `fetch_page`, `fetch_pages`, `index_stats`, `seed_url`, `refresh_page`, nothing more.
    *   `search_web` returns a **plain Markdown document (no JSON envelope)**: a `Source:` / `Degraded:` header (plus `Provider Errors:` when providers failed), then `Title:` / `URL:` / `Snippet:` blocks separated by `---` lines; local hits carry a `Last Crawled:` line per result.
        
    *   `fetch_pages` is the bulk read tool: `urls` + `max_chars` (total budget) + `per_page_chars` + swappable `strategy` (`smart` default, `head`, `tail`, `even`, `priority`). All strategies must be **boundary-safe** (cut on whitespace/newline, never mid-word or mid-tag) and must emit a `[truncated — N chars omitted, strategy=X]` marker per trimmed page.
        
    *   Both read tools **bump** `fetch_count` (priority + prominence); never let a URL be crawled twice concurrently (shared in-flight set).
        
4.  **Never invent configuration values.** Ask Wellington (one consolidated message) for anything missing: `PG_PASSWORD`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `CRAWL4AI_URL` + `CRAWL4AI_API_KEY`, the existing service's database name (for `init/01-databases.sql`), the existing shared Docker network name (check `docker network ls` first), and whether SearXNG is running (keep or drop it from `SEARCH_PROVIDERS`). Ship `.env.example` with placeholders only — real keys live in `.env`, never in the repo.
    
5.  **Verify before wiring:** one live call to each enabled search provider with real keys before `search_web` is built on top; confirm the Crawl4AI auth header format against the running server; verify `fn_search_local` (RRF fusion of FTS + trigram + vector, with `fetch_count`/freshness boosts) by hand in psql.
    
6.  **Follow the build order (§13) in sequence:** 0) infra Postgres → 1) scaffold → 2) schema + `db.py` → 3) `embed.py` + `chunk.py` → 4) `providers/` package + gateway → 5) `index.py` → 6) `queue.py` → 7) `crawler.py` + `truncation.py` + `search_web.py` + `fetch.py` + `worker.py` → 8) `tools.py` + `mcp.py` + `app.py` (REST per §2 incl. `POST /api/fetch-bulk`) → 9) dashboard (`static/index.html`, §9) → 10) OWUI `search_the_web` skill rewrite (§12) → 11) README runbook → 12) end-to-end test pass.
    
7.  Write real, runnable code — no placeholders, no `TODO` bodies. Every file in the §5 repo layout should exist and be wired together when you're done.
    
8.  **Hold yourself to the acceptance criteria (§14)**, especially: immediate response with zero crawl latency (3–5); local hits cost zero provider quota (5); **bulk-fetch truncation** (9); provider failover (10); quota-ledger exhaustion (11); concurrent-dedup (8); and the `degraded: true` local-only fallback when all providers fail (12).
    

### Context Wellington already decided (do not re-ask)

*   SearXNG is **demoted to an optional keyless last-resort** provider — its metasearch scraping is unreliable from server IPs. Wellisearch is the monolith: search gateway + index + cache + crawl orchestration; Crawl4AI is the only external service dependency.
    
*   Free-tier assumptions (June/July 2026): Tavily ≈ 1,000 credits/mo no card; Brave ≈ 1,000 queries/mo via $5 credits, **card required** (free tier retired Feb 2026). Exact numbers are not load-bearing — the quota ledger + failover absorb drift.
    
*   Cross-project compose: no `depends_on` across projects — the app does a **startup DB retry** and **self-creates the** `wellisearch` **DB**; consumers reach Postgres via hostname alias `postgres` on the shared network `owui-net`.
    
*   The embedding model is load-bearing: changing it invalidates all vectors, so ship `python -m wellisearch.reindex` and store the model name per page/chunk row.
    
*   The OWUI `search_the_web` skill will be rewritten to `search_web` + `fetch_page`/`fetch_pages` (plan §12); the old SearXNG MCP is retired after migration.
    

### Deliverables checklist

- [ ] [ ] 

`infra/postgres/` up + healthy (compose, `.env`, `init/01-databases.sql`); `shared` + the existing service's DB present; existing service rewired to it; old Postgres retired.

- [ ] [ ] 

`wellisearch/` repo complete: `compose.yml`, `.env.example`, `Dockerfile`, `README.md`, `sql/schema.sql`, and the full `src/wellisearch/` package per §5.

- [ ] [ ] 

MCP server over stateless Streamable HTTP (`/mcp/http`) exposing exactly the six §7 tools.

- [ ] [ ] 

REST API per §2 (incl. `POST /api/fetch-bulk`, `/api/providers` GET/PATCH).

- [ ] [ ] 

Dashboard at `/` per §9 (live stats, provider quota panel, provider toggle, seed/refresh actions).

- [ ] [ ] 

Worker with debounced kick-on-enqueue, in-flight dedup, budgeted refresh, and `--once` mode.

- [ ] [ ] 

OWUI `search_the_web` skill rewritten per §12.

- [ ] [ ] 

End-to-end pass against §14 — report each criterion as passed / failed / blocked-on-missing-value.

### When you're blocked

*   **Missing value** (key, password, DB name, network name): stop and list everything you need in one message — don't guess.
    
*   **Provider response shape differs from the plan:** adapt in `providers/base.py` (the single normalization point) and note the deviation in the README.
    
*   **A failing acceptance criterion:** never skip silently; document it with repro steps.