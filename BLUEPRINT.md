# wellisearch — Plan & Layout

**Status:** Approved design, ready to build.
**Owner:** Wellington (Las Vegas, self-hosted Docker stack: Open WebUI + Crawl4AI + shared Postgres, LM Studio on host for chat).
**Name:** `wellisearch` (one word; repo name, image name, Postgres DB all `wellisearch`).

---

## 1. What it is

A single self-hosted, self-maintaining **search gateway + web-index service**:

- Takes a web-search query → searches its own auto-updating index of crawled web pages (FTS + trigram + vector hybrid in Postgres). **Local hits cost zero upstream API credits** — the index is a quota-preservation layer, not just a speed optimization.
- On a miss → delegates to **hosted search APIs through a provider gateway**: **Tavily (primary) → Brave (secondary) → SearXNG (optional keyless last-resort)**, in configured priority order with automatic failover on error/quota-exhaustion. (SearXNG is no longer the primary — its metasearch scraping is unreliable from server IPs and was the wrong tool for a programmatic workload.)
- Logs every search (query, source/provider, urls, titles, snippets) to `search_log` for future reference.
- **Returns the provider response to the client immediately** (no crawl latency in the response path), then **enqueues the result URLs into a background crawl queue**. The worker drains the queue (an enqueue kicks an immediate, debounced worker tick) so those pages are chunked + embedded into Postgres within seconds, not minutes.
- Exposes the read tools, `fetch_page(url)` and bulk `fetch_pages(urls, ...)`: return a URL's (or a batch of URLs') content as clean markdown (from index if present, else via Crawl4AI on demand). `fetch_page` bumps `fetch_count` — which both (a) raises that page's priority in the background refresh loop and (b) boosts its prominence in search ranking. `fetch_pages` reads many at once under a shared character budget with **swappable truncation strategies**.
- **The real indexing loop is the read tools:** every page the LLM actually _reads_ gets crawled + stored on demand. The `search_web` queue is the speculative "pre-index the answer" layer on top. Either way the page ends up in the index; the read path is never blocked by a crawl.
- Background worker re-crawls the watchlist continuously (every 30 min tick, budgeted per run), ordered by `fetch_count DESC, last_crawled ASC`. Pages the LLM actually reads get refreshed first and rank higher.
- REST API + **dashboard UI** (same container) showing live activity: current crawl/queue, index size, search hit-rate by provider over time, crawl success/failure, provider quota usage, top-fetched pages, freshness distribution.

**This is the evolution of the OWUI `search_the_web` skill** (SearXNG → Crawl4AI `md`). After this, the LLM's workflow is just: `search_web` (wellisearch) + `fetch_page`/`fetch_pages` (wellisearch). The old SearXNG MCP in OWUI can be retired (wellisearch's provider gateway covers the fallback); keep it registered only during migration.

## 2. REST API (dashboard + scripting)

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness + pg reachable + crawl4ai reachable + provider key presence (per provider, no key values) |
| `GET /api/stats` | index size, page/chunk counts, queue depth, hit-rate 24h/7d/30d **by provider**, quota usage vs limit, worker state, last tick |
| `GET /api/search?query=&k=&format=markdown\|json` | same pipeline as MCP `search_web` (Markdown default; `format=json` or `Accept: application/json` → JSON) |
| `POST /api/fetch` `{url, format?}` | same as MCP `fetch_page` |
| `POST /api/fetch-bulk` `{urls, max_chars?, per_page_chars?, strategy?, format?}` | same as MCP `fetch_pages` (bulk, budgeted truncation) |
| `GET /api/queue` | crawl_queue state (pending/in_flight/done/failed, counts, last few) |
| `GET /api/providers` | gateway state: order, enabled, quota used/limit, last-served, last error per provider |
| `PATCH /api/providers/{name}` | `{enabled: bool, limit: int?}` (runtime toggle, persists to env-backed store) |
| `GET /api/pages?domain=&disabled=&limit=&offset=` | browse watchlist (url,title,counts,timestamps) |
| `POST /api/pages` `{url}` | manual seed → enqueue + kick worker |
| `PATCH /api/pages/{url}` | `{disabled?, fetch_count?}` |
| `DELETE /api/pages/{url}` | remove from index |
| `POST /api/refresh` | kick a worker tick now (queue + refresh, budgeted) |
| `GET /api/logs/search?limit=&offset=` / `GET /api/logs/crawl?limit=&offset=` | history |
| `GET /` | dashboard |

The MCP server and the REST API share the same pipeline code; MCP is the LLM-facing surface, REST is the dashboard/scripting surface. MCP mount: SSE at `/mcp/sse` (OWUI connects to `http://<host>:<port>/mcp/sse`).

## 3. Stack decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Language | **Python 3.12** (FastAPI + MCP Python SDK + uvicorn) | fastembed, Crawl4AI client, psycopg are all first-class Python; one process serves REST + MCP + dashboard + worker. Java rejected (more moving parts, no benefit). |
| Search providers | **Pluggable provider gateway, priority order: `tavily → brave → searxng(optional)`**. Tavily primary (1,000 free credits/mo, **no credit card**, agent-optimized structured results); Brave secondary (~1,000 queries/mo via $5 credits, **card required** — free tier retired Feb 2026; independent 30B+ index); SearXNG demoted to optional keyless last-resort only. | SearXNG-as-primary rejected: metasearch scraping from server IPs → engine blocks/denials, no SLA, wrong shape for programmatic use. Provider APIs are reliable, stable-contract, and free tiers total ~2,000 queries/mo with local hits costing nothing. |
| DB | **Postgres 18 (latest) + pgvector** — shared standalone container in **`infra/postgres/` (outside the wellisearch repo)**, image `pgvector/pgvector:0.8.6-pg18-trixie` (tag verified on Docker Hub, June 2026) | Wellington has a Postgres in another compose service with **no data to migrate** (confirmed). Standalone container serves that service too (extract + re-seed). Infra lives outside the app repo because it outlives any single app. |
| Embeddings | **fastembed `sentence-transformers/all-MiniLM-L6-v2` (384-dim)**, in-process ONNX, CPU | LM Studio = chat only. The doc-embedding model and query-embedding model MUST be identical → one `EMBED_MODEL` constant used by worker and server. No API keys, ~300 MB RAM, headless. |
| Crawler | Existing **Crawl4AI API server** (REST), URL + API key from `.env` passed through Docker. **Single crawling path** — Tavily's extract/answer features deliberately NOT used, so all indexed content comes from one uniform pipeline (fit-markdown). | Already running; `md` fit-markdown is the content format we store. |
| Read truncation | **Swappable truncation strategies** (`smart` default, plus `head`/`tail`/`even`/`priority`) applied when `fetch_pages` hits a char budget; always boundary-safe. | Lets the LLM ask for "the most useful N chars across these pages" without N round-trips; strategies are per-call overridable and a server default. |
| Fallback/discovery | Provider gateway (§1) — **SearXNG container no longer a required dependency**; wellisearch calls it only if `searxng` is in `SEARCH_PROVIDERS` and it's running. | Removes a container from the critical path; keeps keyless insurance if Wellington leaves SearXNG up. |
| Indexing model | **Async.** Search path never blocks on a crawl. Enqueue → background worker drains (enqueue kicks a debounced immediate tick). `fetch_page`/`fetch_pages` crawl on demand as the authoritative read path. | Zero crawl latency in the response; read path is the true priority signal; crawl work shares one bounded parallelism budget. |
| Deploy | **2 containers** in the wellisearch stack: `wellisearch` (app + worker) + shared `postgres`. Crawl4AI is the one external service dependency. No separate worker container, no host cron. Cross-project ordering via **app-side DB retry** (compose `depends_on` doesn't work across projects). | Low RAM (shared 64 GB host). Worker runs as an asyncio task in the app (crawl/embed is I/O-bound). `--once` mode available for manual runs. |
| Searched content | Fit-markdown per page, chunked (~800 tokens), stored per-chunk with embeddings | Hybrid search: GIN FTS + GIN pg_trgm + HNSW cosine, fused in SQL. |

**Rejected alternatives (research done):** Onyx (MIT, does it all natively but ~10 GB RAM / 7 services — too heavy), Karakeep (great MCP archive but no scheduled refresh, AGPL), Qdrant (good hybrid but 2nd DB for no gain at our scale), AnythingLLM/Khoj/Perplexica (wrong shape), SearXNG-as-primary (unreliable server-side scraping). Postgres wins on RAM, single-store, and Wellington's preference.

## 4. Architecture

```
                         ┌──────────────────────────────────────────────┐
   OWUI LLM ──MCP (SSE)─▶│            wellisearch  (one container)      │
                         │  FastAPI app:                                │
   dashboard (browser) ─▶│  • REST API  • MCP tools  • static dashboard │
                         │  • provider gateway (priority + failover     │
                         │    + monthly quota ledger)                   │
                         │  • background worker (asyncio task)          │
                         │      ├─ drains crawl_queue (kicked on enqueue)│
                         │      └─ periodic refresh of watchlist        │
                         └─────────┬──────────────┬─────────────┬───────┘
                                   │              │             │
                 ┌─────────────────▼───┐   ┌──────▼───────┐  ┌──▼───────────┐
                 │ Search APIs (on     │   │ Crawl4AI     │  │ Postgres 18  │
                 │ local miss):        │   │ md endpoint  │  │ + pgvector   │
                 │ Tavily → Brave →    │   │              │  │ + pg_trgm    │
                 │ [SearXNG, optional] │   │              │  │ (infra/postgres)│
                 └─────────────────────┘   └──────────────┘  └──────────────┘
```

## 5. Repo layout

```
# stack root (where Wellington keeps his self-hosted projects)
infra/
└── postgres/                  # SHARED INFRA — outside the app repo (see §11)
    ├── docker-compose.yml
    ├── .env                   # PG_PASSWORD
    └── init/
        └── 01-databases.sql   # creates the existing service's DB on first init

wellisearch/                   # THE APP REPO (opencode builds this)
├── compose.yml                # wellisearch app container (+ env passthrough)
├── .env.example
├── Dockerfile                 # python:3.12-slim; pre-warm fastembed model download
├── README.md                  # runbook: build, up, infra-postgres steps, provider setup, ops, backups
├── sql/
│   └── schema.sql             # extensions + tables + indexes + fn_search_local
└── src/wellisearch/
    ├── __init__.py
    ├── config.py              # pydantic-settings from env (all knobs below)
    ├── db.py                  # pool, DDL apply, fn_search_local call, upsert helpers, startup retry, self-create DB
    ├── embed.py               # fastembed singleton, EMBED_MODEL constant, embed(texts)
    ├── chunk.py               # markdown chunker (~800 tokens, respect headings)
    ├── crawler.py             # Crawl4AI REST client: fit_markdown(url), timeout, API key
    ├── providers/
    │   ├── __init__.py        # GATEWAY: ordered try, failover, quota ledger, normalization
    │   ├── base.py            # Provider.search(query, num) -> list[Result]; Result dataclass
    │   ├── tavily.py          # Tavily Search API (Bearer key; primary)
    │   ├── brave.py           # Brave Search API (X-Subscription-Token; secondary)
    │   └── searxng.py         # SearXNG JSON (optional keyless last-resort)
    ├── queue.py               # crawl_queue: enqueue/dedupe/in-flight set, kick()
    ├── search_web.py          # search_web pipeline (local → GATEWAY → log → ENQUEUE → return)
    ├── fetch.py               # fetch_page + fetch_pages pipelines (index-or-crawl on demand + fetch_count bump; bulk budgeted truncation)
    ├── truncation.py          # swappable truncation strategies (smart/head/tail/even/priority), boundary-safe cuts
    ├── index.py               # store_page(url, markdown): hash, chunk, embed, upsert
    ├── worker.py              # tick: drain queue + budgeted re-crawl by priority, housekeeping
    ├── tools.py               # MCP tool surface: search_web, fetch_page, fetch_pages, index_stats, seed_url, refresh_page
    ├── mcp.py                 # MCP server (SSE): registers tools from tools.py
    ├── app.py                 # FastAPI: REST routes + mount MCP + serve dashboard + start worker
    └── static/
        └── index.html         # dashboard (vanilla JS + auto-refresh; no build step)
```

## 6. Data model (sql/schema.sql)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE pages (
  url            text PRIMARY KEY,
  title          text,
  domain         text,
  fit_markdown   text,
  content_hash   text,               -- sha256 of fit_markdown; unchanged → skip re-embed
  embedding_model text,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_crawled   timestamptz,
  last_status    text,               -- ok | unchanged | error | http_<code>
  crawl_count    int NOT NULL DEFAULT 0,
  fetch_count    int NOT NULL DEFAULT 0,        -- the priority/prominence counter
  search_hit_count int NOT NULL DEFAULT 0,
  disabled       boolean NOT NULL DEFAULT false
);

CREATE TABLE chunks (
  id           bigserial PRIMARY KEY,
  url          text NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
  seq          int  NOT NULL,
  text         text NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding    vector(384),
  last_crawled timestamptz NOT NULL
);
CREATE INDEX chunks_tsv_gin   ON chunks USING gin (tsv);
CREATE INDEX chunks_trgm_gin  ON chunks USING gin (text gin_trgm_ops);
CREATE INDEX chunks_vec_hnsw  ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE crawl_queue (
  id bigserial PRIMARY KEY,
  url text NOT NULL,
  source text NOT NULL,              -- 'search' | 'manual'
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  last_error text,
  status text NOT NULL DEFAULT 'pending'   -- pending | in_flight | done | failed
);
CREATE UNIQUE INDEX crawl_queue_url_pending_uq ON crawl_queue(url) WHERE status IN ('pending','in_flight');

CREATE TABLE search_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  query text NOT NULL,
  source text NOT NULL,              -- 'local' | 'tavily' | 'brave' | 'searxng'
  local_hits int,
  results jsonb                      -- [{url,title,snippet,score?}, ...]
);

CREATE TABLE provider_quota (
  provider text NOT NULL,            -- 'tavily' | 'brave'
  month    text NOT NULL,            -- 'YYYY-MM'
  used     int NOT NULL DEFAULT 0,
  limit    int,                      -- NULL = unknown; gateway still fails over on 429
  PRIMARY KEY (provider, month)
);

CREATE TABLE crawl_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  url text NOT NULL,
  trigger text NOT NULL,             -- 'search' | 'fetch' | 'refresh' | 'manual'
  status text NOT NULL,              -- 'ok' | 'unchanged' | 'error' | 'http_<code>'
  ms int,
  chunks_written int,
  detail text
);
```

### `fn_search_local(query text, qvec vector(384), k int)` — the ranking core
- FTS leg: top ~50 chunks by `ts_rank_cd(tsv, to_tsquery('english', plainto_tsquery('english', query)))` (fall back to `websearch_to_tsquery`).
- Trigram leg: top ~50 chunks by `similarity(text, query)` (covers typos/fragments LLMs emit; cheap insurance).
- Vector leg: top ~50 chunks by `embedding <=> qvec`.
- **RRF fusion** of the three ranked lists (per chunk id).
- Per-page boosts: `× (1 + log1p(p.fetch_count))` (prominence — pages the LLM actually reads) and `× exp(-age_days/14)` (freshness decay, `age` from `last_crawled`).
- Filter `p.disabled = false`.
- Returns: `url, title, snippet (top-scoring chunk text, trimmed to ~400 chars), score, last_crawled, fetch_count`.

## 7. MCP Tool Surface

The MCP server (SSE at `/mcp/sse`) exposes the tools below. `search_web` is the core search tool; `fetch_page` (single) and `fetch_pages` (bulk, budgeted) are the read tools; `index_stats`, `seed_url`, and `refresh_page` are operational conveniences. `search_web`, `fetch_page`, and `fetch_pages` all return a **plain Markdown document by default** (no JSON envelope) defined below; pass `format="json"` (MCP) or `format=json` / `Accept: application/json` (REST) to get the **structured JSON envelope** instead — the `format` param wins over the `Accept` header. The operational tools return structured JSON. The normalization layer (`providers/base.py`) converts every provider's raw shape into this contract — LLMs never see a raw provider payload.

### `search_web(query, num_results=5, max_crawl=5, max_age_days=null, format="markdown")`
The primary search tool. Takes a web-search query and returns a **Markdown document by default**: a response-level header, then one block per result.

**Return — Markdown format (hard contract):**
```
Source: local
Degraded: false

Title: Web Research Title
URL: https://example.com
Last Crawled: 2026-08-20T14:03:11Z
Snippet: This is what the result is about.
---
Title: Next Title
URL: https://example2.com
Snippet: Another snippet.
```
- Header: `Source:` (`local` | `tavily` | `brave` | `searxng` | `error`) and `Degraded:` (`true` | `false` — all providers failed, local-only results served). When providers failed, a `Provider Errors:` line follows (e.g. `tavily: 429; brave: timeout`).
- One block per result; blocks separated by a `---` line.
- Each block has, in order: `Title:`, `URL:`, optional `Last Crawled:` (local hits only, ISO timestamp), `Snippet:`.
- `Snippet` trimmed to ~400 chars.
- `source = error` (nothing to serve): header only, no blocks.

**Parameters:**
- `query` (required): natural-language / web search query.
- `num_results` (default 5): how many results to return.
- `max_crawl` (default 5): how many top result URLs to enqueue for background indexing (0 = pure read, no enqueue).
- `max_age_days` (optional): freshness filter on local hits (only return local pages crawled within N days).
- `format` (default `"markdown"`): `"markdown"` or `"json"` — the wire format. `"json"` returns the structured envelope `{ source, degraded, count, results:[{url,title,snippet,score,last_crawled?,fetch_count?}], provider_errors? }`.

**Pipeline (no crawl in the response path):** local index → (miss) provider gateway → log → enqueue top `max_crawl` → **return immediately**. Local hits cost zero provider credits.

### `fetch_page(url, max_chars=null, format="markdown")`
Loads the content of a **single** URL as **clean/fit Markdown** for the LLM to read.
- Returns the stored `fit_markdown` if present, else crawls on demand via Crawl4AI, stores it, and returns the Markdown.
- **Bumps `fetch_count`** — fetched pages are (a) prioritized in the background crawl/refresh loop and (b) boosted in search prominence.
- `max_chars` (optional): cap on returned content length (default = full content). Boundary-safe cut.
- `format` (default `"markdown"`): `"markdown"` or `"json"` — the wire format. `"json"` returns the structured envelope `{ ok, url, title, markdown, chars, truncated, from_index }` (failed: `{ ok:false, url, error }`).

**Return — Markdown format (hard contract):**
```
Title: How to Deploy Docker Compose Stacks to Remote Hosts
URL: https://example.com/blog/post/docker-compose-remote
From Index: true
Chars: 12875
Truncated: false

# How to Deploy Docker Compose Stacks to Remote Hosts
...page body (the fit Markdown)...
```
- Header (in order): `Title:`, `URL:`, `From Index:` (`true` | `false` — served from the index vs crawled on demand), `Chars:` (body length), `Truncated:` (`true` | `false`).
- A trimmed body ends with its `[truncated — N chars omitted, strategy=head]` marker.
- Failed fetch: `URL:` / `Status: failed` / `Error:` header only (HTTP 200).

### `fetch_pages(urls, max_chars=null, per_page_chars=null, strategy="smart", format="markdown")`
Bulk read of **multiple pages in one call**, under a **shared total character budget**, with **swappable truncation strategies**. Use when the LLM wants to read several `search_web` results at once instead of making N round-trips.
- `urls` (required): list of URLs to fetch in bulk.
- `max_chars` (optional): **total character budget across all pages combined** — the "maximum amount of chars you want." If null, return full content.
- `per_page_chars` (optional): per-page cap (bounds a single page so one long page can't eat the whole budget).
- `strategy` (optional, default `"smart"`): how the budget is allocated and where content is trimmed — **swappable** per call (see strategies below).
- `format` (default `"markdown"`): `"markdown"` or `"json"` — the wire format. `"json"` returns the structured envelope `{ ok, pages_fetched, truncated, total_chars, strategy, budget?, pages:[{url,title,content,chars,truncated,omitted,from_index}] }`.
- **Bumps `fetch_count` for every page fetched** (same priority/prominence effect as `fetch_page`); pages not yet indexed are crawled on demand (parallelized, in-flight-deduped).

**Return — Markdown format (hard contract):**
```
Strategy: even
Budget: 3000
Pages Fetched: 2
Total Chars: 3322
Truncated: true

Title: ...
URL: https://...
From Index: true
Chars: 1500
Truncated: true
---
<content, trimmed to fit the budget, ending with its [truncated — N chars omitted, strategy=X] marker>

Title: ...
URL: https://...
From Index: false
Chars: 1822
Truncated: false
---
<content>
```
- Global header (in order): `Strategy:`, `Budget:` (only when a budget is set — omitted = unlimited), `Pages Fetched:`, `Total Chars:`, `Truncated:`.
- One section per page (in order): `Title:`, `URL:`, `From Index:`, `Chars:` (content chars after trimming), `Truncated:`, then a `---` line and the body.
- Failed/invalid URLs get a `URL:` / `Status: failed` / `Error:` section instead; nothing fetched → the global header carries `Pages Fetched: 0` / `Status: failed` / `Error:` plus one error section per URL (HTTP 200).

**Truncation strategies (swappable):**
- `smart` (default): allocate the budget by page relevance/prominence (`fetch_count`, then search score), and within a page keep the highest-scoring chunks first (from the local index if present; else a heading/lead heuristic). Best signal-per-char.
- `head`: keep the first N chars of each page (article leads) — split across pages.
- `tail`: keep the last N chars of each page (conclusions/summaries) — split across pages.
- `even`: split the budget evenly across pages, `head`-trim each.
- `priority`: split the budget proportionally to `fetch_count`/prominence, `head`-trim each.
The strategy is overridable per-call and has a server default via `FETCH_DEFAULT_STRATEGY` (§10). All strategies are **boundary-safe**: cuts land on the nearest whitespace/newline (never mid-token, mid-word, or mid-HTML tag), and each trimmed page gets a `[truncated — N chars omitted, strategy=X]` marker so the LLM knows more content exists.

### `index_stats()`
Returns a snapshot of the index + gateway state, so the LLM can gauge freshness before relying on it:
- total pages, total chunks, oldest/newest `last_crawled`
- hit-rate (24h/7d/30d) by provider (local / tavily / brave / searxng)
- crawl queue depth (pending / in-flight / done / failed)
- provider quota usage vs limit (current month)

### `seed_url(url)`
Manually adds a URL to the index (queues a crawl + kicks the worker). Returns queue position/status. Use when the LLM wants to save a specific page for later retrieval.

### `refresh_page(url)`
Forces an immediate re-crawl of a single page (bypassing refresh-order priority). Returns the new `last_crawled` + status. Use when a page's content may have changed.

## 8. Background worker (asyncio task in the app)
Two jobs per **tick**:
1. **Drain `crawl_queue`** (search-enqueued / manual urls): process up to the per-tick budget, `CRAWL_MAX_PARALLEL` at a time → Crawl4AI `md` → `store_page()` → mark queue row `done`/`failed` (with `attempts`/`last_error`). Re-enqueue on transient error up to `QUEUE_MAX_ATTEMPTS` (default 3), else `failed`.
2. **Refresh watchlist**: `SELECT url FROM pages WHERE NOT disabled AND (last_crawled IS NULL OR last_crawled < now() - REFRESH_MIN_AGE_HOURS) ORDER BY fetch_count DESC, last_crawled ASC LIMIT WORKER_BUDGET_PER_RUN` → Crawl4AI `md` → hash → **unchanged? skip embed/update** (log `unchanged`) → else re-chunk + re-embed + replace chunks. Update `last_crawled`, `crawl_count`, `last_status`. Log to `crawl_log` (`trigger='refresh'`). Freshness gate (`REFRESH_MIN_AGE_HOURS`, default 72): pages crawled more recently are skipped, so a `seed_url`/search kick only crawls the enqueued URL and genuinely stale pages — never the whole watchlist.

Tick triggers:
- Every `WORKER_INTERVAL_MIN` (default 30) unconditionally.
- **Kicked (debounced)** whenever `crawl_queue` receives items — so `search_web` misses get indexed within seconds, not up to 30 min. Debounce window `KICK_DEBOUNCE_S` (default 5): coalesce a burst of enqueues into one tick.
- Per-tick wall-clock budget (`WORKER_TICK_BUDGET_MIN`, default 15) so a slow site can't stall it.
- Shared **in-flight set** (url → future) across worker, `fetch_page`, `fetch_pages`, and REST so the same URL is never crawled twice concurrently.
- `python -m wellisearch.worker --once` for manual runs (drains queue + one refresh pass).
- Housekeeping (default ON): nothing auto-deleted; `disabled` flag + REST controls only.

## 9. Dashboard (static/index.html, no build step)
Auto-refresh (5 s) via `/api/stats` + logs:
- **Now:** worker tick age, in-flight crawl url, **queue depth + oldest pending**, index size, last search (provider + hit/miss).
- **Trends:** search volume & hit-rate by provider (local / tavily / brave / searxng) 24h/7d/30d; **monthly quota usage per provider vs limit**; crawl ok/unchanged/error; index growth; queue drain time.
- **Tables:** top 20 pages by `fetch_count`; top 20 by `search_hit_count`; freshness distribution (<1d / 1–7d / >30d); recent crawls; recent searches (with provider column).
- **Actions:** manual seed URL, trigger refresh, toggle/disable page, delete page, enable/disable provider, set provider limit.

## 10. Configuration (.env → compose → pydantic-settings)
```dotenv
# postgres (host "postgres" = network alias of the shared infra container, see §11)
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=wellington
POSTGRES_PASSWORD=change-me
POSTGRES_DB=wellisearch
# crawl4ai (existing server)
CRAWL4AI_URL=http://172.17.0.1:11235        # host-routable URL from the container
CRAWL4AI_API_KEY=change-me
# search providers (ordered priority; first success serves)
SEARCH_PROVIDERS=tavily,brave,searxng       # drop "searxng" if not running it
TAVILY_API_KEY=change-me                    # ~1,000 free credits/mo, no card
TAVILY_QUOTA_MONTHLY=1000                   # 0/unset = unknown; gateway still fails over on 429
BRAVE_API_KEY=change-me                     # ~1,000 queries/mo ($5 credits, card required)
BRAVE_QUOTA_MONTHLY=1000
SEARXNG_URL=http://172.17.0.1:8080          # optional keyless last-resort; JSON format must be on
PROVIDER_TIMEOUT_S=20
# embeddings — single source of truth; worker and server must always use the same model
EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBED_DIMS=384
# search
SEARCH_K=5
SEARCH_MAX_CRAWL=5
SEARCH_MIN_SCORE=0.02
STALE_HOURS=72
MAX_CHUNK_TOKENS=800
# fetch_pages truncation (swappable strategies)
FETCH_DEFAULT_STRATEGY=smart       # smart | head | tail | even | priority
FETCH_MAX_CHARS=40000              # default total budget when max_chars omitted (null/0 = unlimited)
FETCH_PER_PAGE_CHARS=12000         # default per-page cap
# worker / queue (async indexing)
WORKER_INTERVAL_MIN=30
WORKER_BUDGET_PER_RUN=25
WORKER_TICK_BUDGET_MIN=15
KICK_DEBOUNCE_S=5
QUEUE_MAX_ATTEMPTS=3
CRAWL_TIMEOUT_S=45
CRAWL_MAX_PARALLEL=3
# server
BIND_PORT=8780
```
`.env` loaded at the compose level (`env_file: .env`) so provider/crawl4ai keys never live in the repo.

## 11. Infra: shared Postgres (OUTSIDE the wellisearch repo)

Location: `infra/postgres/` **sibling to the wellisearch repo** (stack level). Rationale: it now serves two consumers (wellisearch + the existing service) and should outlive any single app. (Alternative if Wellington prefers one repo: `wellisearch/infra/postgres/` — data volume is global to Docker so it survives; but semantically it belongs in `infra/`.)

### `infra/postgres/docker-compose.yml`
```yaml
name: infra-postgres

services:
  pg:
    # tag verified on Docker Hub (June 2026); plain "0.8.6-pg18" is the fallback
    image: pgvector/pgvector:0.8.6-pg18-trixie
    container_name: shared-pg
    restart: unless-stopped
    environment:
      POSTGRES_USER: wellington
      POSTGRES_PASSWORD: ${PG_PASSWORD:?set PG_PASSWORD in infra/postgres/.env}
      POSTGRES_DB: shared            # neutral maintenance DB for a shared infra Postgres
      TZ: America/Las_Vegas
    shm_size: 256mb                      # Docker default 64mb breaks busy pg workloads
    ports:
      - "127.0.0.1:5432:5432"            # host-local ops only (psql/pg_dump); never LAN-exposed
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U wellington -d shared"]
      interval: 5s
      timeout: 3s
      retries: 12
    networks:
      owui-net:
        aliases:
          - postgres                     # consumers use host "postgres" (cross-project DNS)

networks:
  owui-net:
    name: owui-net                       # stable name, independent of compose project name
    # NOTE: if Wellington's OWUI stack already shares a user-defined network
    # (check `docker network ls`), replace the above with:
    #   external: true
    # and name it that network, so all services sit on the same L2.

volumes:
  pgdata:
    name: shared-pg-data                 # stable name, survives re-creation
```

### `infra/postgres/.env`
```dotenv
PG_PASSWORD=<strong-password>
```

### `infra/postgres/init/01-databases.sql`
```sql
-- Runs ONCE on first container init (empty data volume only).
-- `shared` already exists (POSTGRES_DB) and is the maintenance/admin DB.
-- Create each consumer's DB explicitly (fill in the existing service's name):
CREATE DATABASE <existing-service-db>;
CREATE DATABASE wellisearch;    -- optional: the app self-creates this on boot (see below)
```

### Self-creating consumer DBs
`shared` is only the guaranteed maintenance DB. Each consumer gets its own DB. The wellisearch app, on startup, connects to `shared` as the admin user and idempotently ensures its working DB exists before opening its main pool:
```
1. connect POSTGRES_HOST:5432/db=shared user=wellington
2. IF NOT EXISTS (select 1 from pg_database where datname='wellisearch')
     CREATE DATABASE wellisearch;
3. open the main pool against db=wellisearch; run DDL (extensions, tables, fn_search_local)
```
This makes the app self-sufficient even if the init script is skipped; `POSTGRES_DB=shared` only needs to be *some* real DB.

### Cross-project wiring (why alias + named network, not depends_on)
- Compose `depends_on` does **not** work across projects → the wellisearch app does a **startup DB retry** (~10 attempts × 3 s) instead; log "waiting for postgres…" while retrying.
- Any consumer joins `owui-net` (external) and reaches Postgres at hostname **`postgres`** (the network alias). `container_name: shared-pg` is for human ops (`docker exec -it shared-pg psql -U wellington`).
- Wellisearch `compose.yml` therefore has **no depends_on on postgres**, only the external network:
```yaml
services:
  wellisearch:
    build: .
    restart: unless-stopped
    env_file: .env
    ports: ["8780:8780"]
    networks: [owui-net]
networks:
  owui-net: {name: owui-net, external: true}
```

### Migration of the existing service (no data to migrate)
1. `cd infra/postgres && docker compose up -d` → network created, Postgres up, init script runs.
2. Verify: `docker compose exec pg psql -U wellington -c '\l'` → lists `shared`, `wellisearch` (after first app boot), + the other service's DB.
3. Existing service's compose: add `owui-net` (external) to its service, point its DB host at `postgres` (alias); keep user/password/db names unchanged. Bring it up; smoke-test it.
4. Stop + delete the old Postgres service and its volume.
5. Wellisearch builds/runs against it.

### Ops
- Backups: `docker compose exec -T pg pg_dumpall -U wellington | gzip > backups/all-$(date +%F).sql.gz` (cron or manual). Per-DB: `pg_dump -U wellington <db>`.
- The wellisearch app is the only consumer that needs pgvector; `pg_trgm` too. Both are created by the app at startup (`CREATE EXTENSION IF NOT EXISTS`).

## 12. OWUI skill rewrite (`search_the_web.md`)
New workflow (replaces the old SearXNG→md steps):
1. **Search** — call wellisearch `search_web(query, num_results: 5)`. Returns immediately a Markdown document: a `Source` (`local`|`tavily`|`brave`|`searxng`) / `Degraded` header, then `Title`/`URL`/`Snippet` blocks (local hits carry a `Last Crawled` line). (On a miss, the result urls are being indexed in the background — no need to wait. If the header carries `Degraded: true`, results are local-only; retry with a rephrased query or note the limitation.)
2. **Read** — for a single page call `fetch_page(url)`; for several at once call `fetch_pages(urls, max_chars)` with a char budget and a `strategy` (default `smart`) (these are the only "read a page" tools; never call Crawl4AI `md` directly anymore).
3. **Evaluate** — if the answer is thin, reformulate (≤2 tries) and `search_web` again.
4. **Synthesize** — answer from `fetch_page`/`fetch_pages` content, cite URLs; note source dates from `last_crawled` for time-sensitive topics.
Rules: never invent page content; if `fetch_page`/`fetch_pages` fails, say which URL failed and fall back to `search_web` with a rephrased query.
The old SearXNG MCP in OWUI may be retired once wellisearch is in service (its provider gateway covers the fallback); keep it only during migration.

## 13. Build order (opencode task list)
- [ ] **0. Infra first (Wellington or opencode):** create `infra/postgres/` (compose + `.env` + init script) per §11; `docker compose up -d`; verify DBs exist; wire the existing service to it; retire the old Postgres. Check `docker network ls` first for an existing shared network.
- [ ] **1. Scaffold wellisearch repo:** `compose.yml` (external network, no cross-project depends_on), `.env.example`, `Dockerfile`, `README.md`, package (`pyproject.toml`, deps: fastapi, uvicorn, mcp, psycopg[binary], fastembed, httpx, pydantic-settings)
- [ ] **2. `sql/schema.sql`** (extensions, tables incl. `crawl_queue` + `provider_quota`, indexes, `fn_search_local`) + `db.py` (pool with **startup retry**, self-create `wellisearch` DB, DDL on startup, helpers) — verify `fn_search_local` manually in psql
- [ ] **3. `embed.py`** (fastembed singleton) + `chunk.py` (markdown chunker) — unit-test chunking on a sample article
- [ ] **4. `providers/` package:** `base.py` (Result shape + Provider interface), `tavily.py`, `brave.py`, `searxng.py`, gateway (`__init__.py`: ordered failover, `PROVIDER_TIMEOUT_S`, quota-ledger check/increment, per-provider error capture) — **test one live call to each enabled provider with real keys before wiring into `search_web`**; normalize all three to the same Result shape
- [ ] **5. `index.py` `store_page()`** (hash → chunk → embed → upsert, transactional, `unchanged` short-circuit)
- [ ] **6. `queue.py`** (enqueue/dedupe/in-flight set + debounced `kick()`)
- [ ] **7. `crawler.py`** (Crawl4AI REST, API key header — **verify header format against the running server**) + `truncation.py` (swappable strategies `smart`/`head`/`tail`/`even`/`priority`, boundary-safe cuts) + `search_web.py` (local → GATEWAY → log → ENQUEUE → return immediately, emit the §7 `results` Markdown block) + `fetch.py` (`fetch_page` single + `fetch_pages` bulk w/ budget & strategy, await in-flight, `fetch_count` bump) + `worker.py` (drain queue + budgeted refresh, `--once`)
- [ ] **8. `tools.py` + `mcp.py`** (expose exactly `search_web`, `fetch_page`, `fetch_pages`, `index_stats`, `seed_url`, `refresh_page` over SSE, per §7) + `app.py` (REST routes per §2 incl. `/api/fetch-bulk`, dashboard mount, start worker, `/health`)
- [ ] **9. `static/index.html`** dashboard (incl. provider quota panel + provider toggle)
- [ ] **10. Rewrite OWUI `search_the_web` skill** (§12)
- [ ] **11. README runbook:** infra/postgres bring-up + migration steps (§11), provider setup (keys, free tiers, quota behavior), first-run model download note, ops (logs, backups, reindex-on-model-change script `python -m wellisearch.reindex`)
- [ ] **12. End-to-end test pass** (§14)

## 14. Acceptance criteria (test pass)
1. `infra/postgres` up + healthy; `\l` shows `shared` + the existing service's DB; existing service talks to it; old Postgres retired.
2. wellisearch `docker compose up` → `/health` ok (pg + crawl4ai reachable, provider keys present), including the startup-retry path when the app boots before pg.
3. MCP `search_web("what is pgvector")` first call → `Source: tavily` (primary), returns **immediately** (no crawl latency); body is a well-formed Markdown document (`Source`/`Degraded` header + Title/URL/Snippet blocks + `---` separators); `search_log` row written with provider; `crawl_queue` has ≤5 `pending` rows.
4. Within a few seconds (kicked tick), `crawl_log` shows `trigger=search` `ok` rows and `pages`/`chunks` are populated.
5. Same query again → `source: local`, returns in <1 s, snippets from stored chunks, `provider_quota` NOT incremented for local hits.
6. `fetch_page` on a URL not in index → returns clean markdown; `fetch_count=1`; page in `pages`.
7. `fetch_page` on an indexed URL again → returns stored markdown, `fetch_count` increments.
8. **Concurrent-dedup:** fire `fetch_page(url)` while a `search_web`-queued crawl of the same `url` is in flight → second call awaits the first (no double-crawl); `crawl_log` shows a single crawl for that url.
9. **Bulk fetch + truncation:** `fetch_pages([u1,u2,u3], max_chars=6000, strategy="even")` → combined Markdown with one section per page; `total_chars <= 6000`; each cut lands on a newline/whitespace boundary (no mid-word/mid-tag); each trimmed page carries a `[truncated — N chars omitted, strategy=even]` marker; `fetch_count` bumped for all three pages. Switch `strategy` to `smart`/`head`/`tail`/`priority` and confirm allocation/trimming changes as specified.
10. **Provider failover:** set `TAVILY_API_KEY` to an invalid value → next miss served by `source: brave`, both attempts visible in logs; restore key → tavily serves again.
11. **Quota ledger:** set `BRAVE_QUOTA_MONTHLY=2`, exhaust it with two misses → third miss fails over (tavily first); `provider_quota.used` reflects counts.
12. **SearXNG last-resort (if running):** invalidate both keys → misses served by `source: searxng`; if SearXNG not running either → `degraded: true` local-only results (when any exist) or structured error.
13. **Tool surface:** `index_stats()` returns pages/chunks/freshness/hit-rate/queue/quota; `seed_url(url)` enqueues + kicks; `refresh_page(url)` re-crawls a single page and updates `last_crawled`.
14. Search with a **misspelled term** (e.g. "pgvectr") still returns the page (trigram/vector leg).
15. Search for an **exact technical string** present in a stored page ranks it top (FTS leg).
16. Worker: `UPDATE pages SET last_crawled = now() - interval '10 days'` → next tick re-crawls the highest-`fetch_count` page first; unchanged content logs `unchanged` without re-embedding.
17. Dashboard: live stats update (incl. queue depth + provider quotas), top-fetched list, recent searches/crawls visible; provider toggle + manual seed + refresh buttons work.
18. RAM: wellisearch container idle < ~500 MB; Postgres idle < ~300 MB.
19. 100k-page scale note (design check, not test): 100k pages × ~4 chunks × 384-dim ≈ 500k vectors ≈ 0.75 GB + HNSW ≈ 1–1.5 GB total in Postgres — comfortably inside pgvector's easy zone (millisecond range up to a few hundred thousand vectors).

## 15. Risks / verify-at-build
- **Search-provider free tiers are moving targets (verified June/July 2026):** Tavily = 1,000 credits/mo, no card, resets 1st of month; Brave = ~1,000 queries/mo via $5 credits, **card required** (pure-free tier retired Feb 2026). Re-check limits before relying on exact numbers; the quota ledger + failover make exactness non-critical.
- **Provider result schema drift** — each provider returns a different shape; `providers/base.py` is the single normalization point into the §7 `search_web` contract. Version the adapters; keep raw response in debug logging for the first month.
- **`fetch_pages` budget edge cases** — a single page longer than the whole budget must be capped (per-page cap) and marked truncated; empty/duplicate URL lists must be deduped and validated (reject non-http(s)); `smart` strategy needs the local index scores — fall back to `even`+`head` when scores are absent.
- **Brave key requires card** — if Wellington objects to a card on file, make Brave optional (`SEARCH_PROVIDERS=tavily,searxng`) or set Brave's monthly credit cap to $5.
- **All providers exhausted mid-month** — gateway degrades to local-only (`degraded: true`) or structured error; SearXNG is the optional keyless backstop only if Wellington keeps it running.
- **Crawl4AI auth header format** — confirm against the live server (likely `Authorization: Bearer <key>` on v0.9+ secure-by-default; could be `x-api-key`). Test one call before wiring the worker.
- **Existing shared network name** — check `docker network ls` before first `up`; if the OWUI stack already has one, make `owui-net` external and point at it (§11).
- **SearXNG JSON format** must be enabled if kept as fallback (optional).
- **fastembed model download** (~90 MB from HuggingFace on first run) — either let first run download (log it) or bake into image build; document offline re-run.
- **Embedding model is load-bearing**: changing `EMBED_MODEL` invalidates all vectors → provide `python -m wellisearch.reindex` (re-embed all chunks) and store model name per row.
- **HNSW index build** on first big import is memory-hungry but fine for our scale; document `maintenance_work_mem` if needed.
- **Cross-project compose ordering** — no `depends_on` across projects; wellisearch app MUST retry DB connection at startup and self-create its DB (§11).
- **Async queue durability:** `crawl_queue` is in Postgres, so a crash mid-drain loses nothing (pending rows survive restart); in-flight set is in-memory and rebuilt on boot (reset `in_flight` → `pending` on startup).

## 16. Out of scope (for now)
- Multi-user/auth on the wellisearch API (LAN trust; add later if exposed).
- **Paid search tiers** (Tavily $ plans, Brave paid) — enabling is a key + limit change, no code change; skip until free tiers are clearly insufficient.
- Extra providers (Serper, Exa, Google CSE) — drop-in via `providers/` + one env var; add only if needed.
- Semantic "dedup similar pages", site allow/deny lists, per-domain crawl politeness (rate-limit exists via parallelism cap only).
- Knowledge-graph / entity extraction (R2R-style) — revisit if needed.
- Replacing OWUI itself.

---
*Research basis (June–July 2026): Onyx 4/5 but ~10 GB RAM; Karakeep 4/5 archive w/ MCP but no scheduler; ArchiveBox 5/5 on scheduled re-crawl but keyword-only; AnythingLLM/Khoj/Perplexica wrong shape; Qdrant good hybrid but redundant 2nd store. Search providers: Tavily free = 1,000 credits/mo no card (agent-optimized); Brave free ≈ 1,000 queries/mo, card required after Feb 2026 free-tier change; SearXNG demoted to optional keyless fallback (unreliable server-side metasearch scraping). Postgres 18 + pgvector + fastembed chosen for ~1 GB total new RAM overhead and single-store simplicity. Indexing is async by design: search returns immediately, a debounced worker tick drains the crawl queue, and the read tools are the authoritative on-demand read/index path. MCP surface = `search_web` (returns a Title/URL/Snippet Markdown block), `fetch_page` (single read), `fetch_pages` (bulk read under a char budget with swappable `smart`/`head`/`tail`/`even`/`priority` truncation), plus `index_stats`, `seed_url`, `refresh_page`. Shared Postgres lives at `infra/postgres/` (stack level) on a named network with a `postgres` alias so any consumer project can reach it.*
