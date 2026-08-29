# wellisearch — reference documentation

wellisearch is a **self-hosted web-search gateway for LLMs**: a single FastAPI
container that keeps a local, embeddable index of web pages and serves search
results from it first, falling back to paid search providers (Tavily → Brave)
only on a local miss. It exposes the same pipeline as a **REST API**
and an **MCP server** (Streamable HTTP), with a built-in static dashboard.

Design goals (see `BLUEPRINT.md` for the full plan):

- **Quota preservation** — a local hit costs zero provider credits; the index
  is the cache and the gateway is the fallback.
- **One crawling path** — every crawl goes through Crawl4AI `POST /md`
  (fit-markdown). No raw-HTML scraping anywhere.
- **One pipeline** — REST and MCP share the same handler functions
  (`search_web.py`, `fetch.py`, `queue.py`); the dashboard is a thin client.
- **Boring runtime** — one container, one process, one Postgres, one asyncio
  worker task. No queue broker, no object store.

## Contents

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | System topology, process model, component inventory, how a request flows through the container. |
| [search-pipeline.md](search-pipeline.md) | `search_web` step by step: local-first, threshold, gateway failover, degraded mode, logging, speculative indexing. |
| [ranking.md](ranking.md) | `fn_search_local` internals: the three legs, RRF fusion, top-3 cap, prominence and freshness, worked score examples, and the `coverage` local-vs-gateway gate. |
| [data-model.md](data-model.md) | Every table, column, index and stored function in `schema.sql`, with an ER diagram. |
| [indexing.md](indexing.md) | Triggers → `crawl_queue` → worker tick → Crawl4AI → `store_page` (chunk/embed/upsert), in-flight dedupe, the `unchanged` short-circuit. |
| [api.md](api.md) | REST endpoint reference and the six MCP tools, with request/response shapes. |
| [deployment.md](deployment.md) | Docker/compose, shared Postgres and network, full configuration reference, operations (health, reindex, manual worker run). |
| [trigram-rewrite.md](trigram-rewrite.md) | 2026-08 post-mortem: why the trigram leg of `fn_search_local` was rewritten (full-corpus scans → index-bounded), plus the pool/CPU hygiene fixes. |

## Diagrams

All diagrams are inline SVG in `images/` and render directly in Markdown
viewers that support SVG (GitHub, VS Code, Obsidian):

- `images/architecture.svg` — system topology
- `images/search-pipeline.svg` — search flow (local → gateway → fallback)
- `images/ranking.svg` — `fn_search_local` internals
- `images/data-model.svg` — ER diagram
- `images/indexing.svg` — indexing pipeline and worker

## Quick orientation (5-minute tour)

1. **A search request** arrives at `GET /api/search` or the MCP `search_web`
   tool. Both call `search_web()` in `src/wellisearch/search_web.py`.
2. The query is embedded (fastembed, 384-d) and ranked against the local
   index by the Postgres function `fn_search_local` (hybrid FTS + trigram +
   vector, RRF-fused). If any result covers ≥ `LOCAL_MIN_COVERAGE` (default
   `0.75`) of the query's content words, the local rows are served
   immediately — **zero provider credits**.
3. Otherwise the **provider gateway** (`providers/`) tries
   `SEARCH_PROVIDERS` in order (default `tavily,brave`), gated by
   runtime toggles, configuration, and a monthly quota ledger. First
   non-empty result serves; the top result URLs are **enqueued for
   background indexing** so the next identical query is free.
4. **All paths are logged** to `search_log`; every crawl is logged to
   `crawl_log` with trigger, status, and timing.
5. The **background worker** (one asyncio task) drains the crawl queue and
   refreshes the most-fetched pages on a 30-minute tick (or a debounced kick
   when the queue receives work). Crawls go through Crawl4AI, then
   `store_page()` chunks, embeds, and upserts — transactionally.
6. **Reading pages** (`fetch_page` / `fetch_pages`) returns stored
   fit-markdown when indexed, else crawls on demand and stores it; bulk
   reads run under a shared character budget with swappable truncation
   strategies.

## Where things live

```
src/wellisearch/
  app.py            FastAPI routes, auth middleware, worker startup, MCP mount
  search_web.py     the search pipeline (shared by REST + MCP)
  fetch.py          fetch_page / fetch_pages (stored-first, budgeted)
  tools.py          the six MCP tools
  mcp.py            MCP server setup (Streamable HTTP)
  providers/        gateway: tavily, brave adapters + failover
  index.py          store_page: hash → chunk → embed → upsert
  chunk.py          markdown chunker (≤ MAX_CHUNK_TOKENS)
  embed.py          fastembed singleton (EMBED_MODEL / EMBED_DIMS)
  crawler.py        Crawl4AI client (the single crawling path)
  queue.py          crawl_queue enqueue/dedupe + in-flight set + kick
  worker.py         background worker (drain queue + watchlist refresh)
  db.py             psycopg pool + all SQL helpers
  truncation.py     fetch_pages budget allocation strategies
  config.py         pydantic-settings (every env knob, with defaults)
  schema.sql        DDL + fn_search_local (applied at startup)
  reindex.py        full re-embed after an embedding-model change
  loopfix.py        Windows selector-event-loop factory for uvicorn
  static/           dashboard (vanilla JS, no build step)
tests/
  test_units.py     chunking/truncation/gateway logic (no network)
  test_db.py        schema + fn_search_local + quota helpers (needs Postgres)
  e2e_test.py       live end-to-end against the running container
```
