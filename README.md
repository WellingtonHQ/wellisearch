# wellisearch

Self-hosted search gateway + web-index service for the OWUI agent.

One service, two surfaces (same pipeline code underneath):

- **MCP** (SSE at `http://<host>:8780/mcp/sse`) — exactly six tools:
  `search_web`, `fetch_page`, `fetch_pages`, `index_stats`, `seed_url`, `refresh_page`
- **REST + dashboard** — `http://<host>:8780/` (dashboard), `GET /api/search`,
  `POST /api/fetch`, `POST /api/fetch-bulk`, `GET /api/stats`, provider/page
  admin, `/health`

How it works:

1. `search_web` checks the **local index first** (hybrid: FTS + trigram +
   vector, RRF-fused, prominence + freshness boosted). Local hits cost **zero
   provider credits**.
2. On a miss, the **provider gateway** fails over `tavily → brave → searxng`
   (ordered by `SEARCH_PROVIDERS`), with a per-provider monthly quota ledger
   and per-provider error capture.
3. Top result URLs are **enqueued for background indexing** (Crawl4AI `md`
   endpoint) — the search path never blocks on a crawl.
4. The **read tools are the authoritative indexing loop**: `fetch_page` /
   `fetch_pages` return stored markdown if indexed, else crawl on demand and
   store. Every read bumps `fetch_count` (priority + prominence).
5. The **worker** (asyncio task in the app) drains the queue (kicked,
   debounced) and refreshes the watchlist (`fetch_count DESC`, oldest crawl
   first), bounded by a per-tick budget and parallelism cap.

## Requirements

- Docker (app container)
- The shared infra Postgres running (project `infra`) — DB auto-created
- Crawl4AI server running (existing) — the only crawling path
- SearXNG with `format=json` enabled (optional last-resort provider)
- Optional: provider keys (Tavily / Brave) — without them the gateway falls
  back to SearXNG; with none of the three, `search_web` returns
  `Degraded: true` local-only results (or `Source: error` with `Provider
  Errors` on an empty index)

## Setup

```bash
# 1. config
cp .env.example .env
#    fill in real values (keys live in your other .env files —
#    e.g. open-webui/.env holds SEARXNG_BRAVE_API_KEY)

# 2. build + up (joins wellington_default + postgres-net, both external)
docker compose up -d --build

# 3. watch startup (schema auto-applied; DB `wellisearch` auto-created)
docker logs -f wellisearch
```

First-run note: the embedding model
(`sentence-transformers/all-MiniLM-L6-v2`, ~90 MB) is **pre-downloaded in the
image** at build time. A non-Docker install downloads it on first embed.

## Provider setup

| Provider | Key | Free tier (June–July 2026) | Role |
|---|---|---|---|
| Tavily | `TAVILY_API_KEY` | ~1,000 credits/mo, no card | primary (agent-optimized) |
| Brave | `BRAVE_API_KEY` | ~1,000 queries/mo (free-tier change Feb 2026) | secondary |
| SearXNG | none (URL) | — | keyless last resort |

Quota behavior: each provider has a monthly ledger (`provider_quota`) seeded
from `*_QUOTA_MONTHLY` (0/unset = unknown). The gateway checks the ledger
before calling a provider, bumps it on every successful serve, and fails over
on any error (including 429) even when the limit is unknown. Runtime override:
`PATCH /api/providers/{name} {"limit": N}` or the dashboard.

## MCP (OWUI)

Register in OWUI as an SSE MCP server pointing at:

```
http://wellisearch:8780/mcp/sse
```

(from the OWUI container network; `http://127.0.0.1:8780/mcp/sse` from the
host). Tools:

- `search_web(query, num_results=5, max_crawl=5, max_age_days=null)` →
  a Markdown document: `Source:`/`Degraded:` header (+ `Provider Errors:`
  when providers failed), then `Title:`/`URL:`/`Snippet:` blocks separated
  by `---`; local hits carry a `Last Crawled:` line per result
- `fetch_page(url, max_chars=null)` → clean markdown of one page
- `fetch_pages(urls, max_chars=null, per_page_chars=null, strategy="smart")`
  → combined markdown under a shared char budget. Strategies: `smart`
  (prominence-weighted, default), `head`, `tail`, `even`, `priority`.
  Trims are boundary-safe; each trimmed page carries a
  `[truncated — N chars omitted, strategy=X]` marker.
- `index_stats()` → index + gateway snapshot
- `seed_url(url)` → queue a crawl + kick the worker
- `refresh_page(url)` → immediate re-crawl of one page

## REST (quick reference)

| Method & path | Purpose |
|---|---|
| `GET /api/search?query=&k=5` | same pipeline as `search_web` |
| `POST /api/fetch` `{url, max_chars?}` | same as `fetch_page` |
| `POST /api/fetch-bulk` `{urls, max_chars?, per_page_chars?, strategy?}` | same as `fetch_pages` (omitted `max_chars` = server default budget; `null`/`0` = unlimited) |
| `GET /api/stats` | dashboard payload (runtime, trends, quota, queue) |
| `GET /api/pages?sort=fetch_count\|search_hit_count\|last_crawled\|first_seen&limit=20` | top pages + freshness distribution |
| `GET /api/providers` · `PATCH /api/providers/{name}` | gateway state · toggle / set limit |
| `POST /api/seed` `{url}` · `POST /api/refresh` `{url}` | manual indexing |
| `PATCH /api/pages/{url}` `{disabled}` · `DELETE /api/pages/{url}` | index admin |
| `GET /api/logs/crawls?limit=` · `GET /api/logs/searches?limit=` | recent activity |
| `GET /health` | db + crawl4ai + provider health |

Auth: set `WELLISEARCH_API_KEY` to require `Authorization: Bearer <key>` or
`X-API-Key: <key>` on `/api/*` and `/mcp/*` (empty = open).

## Ops

```bash
docker logs -f wellisearch                     # app + worker logs
docker compose restart wellisearch             # worker resets in-flight queue rows on boot
docker exec wellisearch python -m wellisearch.worker --once   # manual drain + refresh pass
docker exec wellisearch python -m wellisearch.reindex         # re-embed after EMBED_MODEL change
docker exec wellisearch python -m wellisearch.reindex --force # re-embed everything
```

- **Model change**: `EMBED_MODEL` is load-bearing — changing it invalidates
  stored vectors. Update `.env`, then `reindex` (or `--force`), then restart.
- **Backups**: data lives in the shared infra Postgres (DB `wellisearch`) —
  covered by the infra project's backup job; no separate backup path here.
- **Housekeeping**: nothing is auto-deleted; pages are `disabled`/deleted via
  REST/dashboard only.
- **Queue**: `crawl_queue` is durable across restarts; stuck `in_flight` rows
  reset to `pending` on boot.

## Configuration

All knobs in `.env` (see `.env.example` for the full annotated list):
Postgres, Crawl4AI URL/key, provider order + keys + timeouts + monthly quotas,
`EMBED_MODEL`/`EMBED_DIMS`, search (`SEARCH_K`, `SEARCH_MAX_CRAWL`,
`SEARCH_MIN_SCORE`, `STALE_HOURS`, `MAX_CHUNK_TOKENS`), fetch
(`FETCH_DEFAULT_STRATEGY`, `FETCH_MAX_CHARS`, `FETCH_PER_PAGE_CHARS`), worker
(`WORKER_INTERVAL_MIN`, `WORKER_BUDGET_PER_RUN`, `WORKER_TICK_BUDGET_MIN`,
`KICK_DEBOUNCE_S`, `QUEUE_MAX_ATTEMPTS`, `CRAWL_TIMEOUT_S`,
`CRAWL_MAX_PARALLEL`), server (`BIND_PORT`, `WELLISEARCH_API_KEY`).

## Troubleshooting

- **`source: searxng` when Tavily/Brave should serve** — check
  `GET /api/providers` for `last_error` per provider (auth, quota exhausted,
  network). The gateway always fails over; the error chain is captured.
- **Search works but index is empty** — worker not draining? Check
  `GET /api/stats` → `runtime.worker.last_tick_stats` and `crawl_queue`
  depth; run `worker --once` manually and read the logs.
- **Crawl4AI 401** — auth header is `Authorization: Bearer <CRAWL4AI_API_KEY>`
  (verified against the running server; `x-api-key` is rejected).
- **Slow first search** — the query embedding loads the model on first use
  (pre-warmed in the Docker image).
- **Dashboard 401** — set the API key in the dashboard header bar (stored in
  localStorage).
- **Windows dev: "Psycopg cannot use the 'ProactorEventLoop'"** — psycopg's
  async needs a selector loop; uvicorn 0.36+ forces the proactor loop on
  Windows. Use the `wellisearch` entry point (it selects it automatically) or
  `uvicorn wellisearch.app:app --loop wellisearch.loopfix:loop_factory`.
  No effect under Docker (Linux).
