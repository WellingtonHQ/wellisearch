# API reference

wellisearch exposes one pipeline through two transports (REST and MCP) plus
a dashboard. REST and MCP share the same handler functions, so behavior is
identical.

Base URL (default): `http://localhost:8780`.

## Authentication

When `WELLISEARCH_API_KEY` is set (recommended), every `/api/*` and `/mcp/*`
request must carry the key:

```
Authorization: Bearer <WELLISEARCH_API_KEY>
```

or

```
X-API-Key: <WELLISEARCH_API_KEY>
```

Comparison is constant-time. An unset key means the API is open (fine for a
trusted LAN; set it for anything reachable). `/health` and the dashboard (`/`)
are always open.

## REST endpoints

### `GET /health`
Liveness + dependency probe. Returns `status` (`ok` | `degraded`), `database`,
`crawl4ai`, and per-provider `configured`/`state`. No key required.

### `GET /api/search`
The search pipeline.

| Param | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | the search text |
| `k` | int | `SEARCH_K` (5) | max results |
| `max_crawl` | int | `SEARCH_MAX_CRAWL` (5) | how many gateway result URLs to index in the background |
| `max_age_days` | float | unset | drop local rows crawled older than this (never-crawled kept) |

Returns the Markdown search document (`Content-Type: text/markdown`, no JSON
envelope — see [search-pipeline.md](search-pipeline.md)): a `Source:` /
`Degraded:` header (+ `Provider Errors:` when providers failed), then
`Title:` / `URL:` / `Snippet:` blocks separated by `---` lines; local hits
carry a `Last Crawled:` line per result. `Source: error` maps to **HTTP 502**
(body is the Markdown header).

### `POST /api/fetch`
Read one URL as fit markdown (stored-first, else crawl+store).

Body: `{ "url": "...", "max_chars": 12000 }` (`max_chars` optional).

Returns: `{ ok, url, title, markdown, chars, truncated, from_index }`.
Bumps the page's `fetch_count`.

### `POST /api/fetch-bulk`
Bulk read under a shared character budget.

Body:
```json
{
  "urls": ["https://…", "https://…"],
  "max_chars": 40000,
  "per_page_chars": 12000,
  "strategy": "smart"
}
```
`max_chars`/`per_page_chars` omitted → server defaults; explicit `0`/`null` →
unlimited. `strategy` ∈ `smart | head | tail | even | priority`.

Returns: `{ ok, markdown, total_chars, pages_fetched, truncated, strategy,
budget, pages:[{url, chars_used, truncated, omitted, from_index}] }`.

### `GET /api/stats`
Dashboard payload: index counts, freshness buckets, queue depth, provider
quota, worker runtime (last tick + stats + in-flight), last search.

### `GET /api/providers`
Per-provider gateway state: `configured`, `enabled`,
`limit_runtime`/`limit_default`, `quota_used`/`quota_limit`, `last_served`,
`last_error`.

### `PATCH /api/providers/{name}`
Runtime toggle / limit override (persists in `provider_state`).

Body: `{ "enabled": false }`, `{ "limit": 500 }`, or both.
`404` if the provider name isn't in `SEARCH_PROVIDERS`.

### `POST /api/seed`
Queue a URL for background indexing and kick the worker.
Body: `{ "url": "..." }`. Returns `{ ok, url, newly_queued }`.

### `POST /api/refresh`
Force an immediate re-crawl of one URL (bypasses queue order).
Body: `{ "url": "..." }`. Returns `{ ok, url, status, chunks, ms, last_crawled }`.
Crawl failure → `502`.

### `GET /api/pages`
List indexed pages.

| Param | Default | Notes |
|---|---|---|
| `sort` | `fetch_count` | `fetch_count` \| `search_hit_count` \| `last_crawled` \| `first_seen` |
| `limit` | 20 | max 100 |

Returns `{ pages: [...], freshness: { "<1d","1-7d","7-30d",">30d","never" } }`.

### `PATCH /api/pages/{url}`
Soft-disable / re-enable a page (excludes it from results without deleting).
URL is percent-encoded in the path. Body: `{ "disabled": true }`. `404` if not indexed.

### `DELETE /api/pages/{url}`
Hard-delete a page (chunks cascade). `404` if not indexed.

### `GET /api/logs/crawls`
Recent crawl attempts. `?limit=` (default 50, max 500).
Returns `{ crawls: [{ ts, url, trigger, status, ms, chunks_written, detail }] }`.

### `GET /api/logs/searches`
Recent searches. `?limit=` (default 50, max 500).
Returns `{ searches: [{ ts, query, source, local_hits, results }] }`.

### `GET /api/window`
Windowed activity stats for the dashboard.

| Param | Default | Notes |
|---|---|---|
| `secs` | 86400 | clamped to 600 (10m) .. 86400 (24h) |

Returns:
```json
{
  "secs": 86400,
  "searches": { "total": 27, "by_source": { "local": 21, "brave": 5 }, "local_rate": 0.778 },
  "crawls": { "total": 623, "by_status": { "ok": 269, "unchanged": 350 } }
}
```

### `GET /api/logs`
Merged windowed log stream (crawls + searches + operational events), newest
first. The dashboard's "Log" view renders this.

| Param | Default | Notes |
|---|---|---|
| `secs` | 86400 | clamped to 600 (10m) .. 86400 (24h) |
| `limit` | 200 | max 500 |

Returns `{ logs: [{ ts, kind: "crawl" \| "search" \| "event", message, info }],
total, secs }` where `total` is the row count in the window before `limit`.

Operational events (worker ticks, provider gateway failures/serves, admin
actions, startup) are written to the `event_log` table by the service itself
and appear here with `kind: "event"`. Log tables are pruned after
`LOG_RETENTION_DAYS` (default 30).

### `GET /`
Serves `static/index.html` (the dashboard). Mounted last (catch-all) so it
never shadows `/api/*` or `/mcp/*`.

## MCP tools (6)

Transport: **SSE**, mounted at `/mcp` → `GET /mcp/sse` (event stream) +
`POST /mcp/messages/` (JSON-RPC). Connect an MCP client to
`http://localhost:8780/mcp/sse`.

All six tools call the same code as their REST counterparts.

| Tool | Purpose | Params |
|---|---|---|
| `search_web` | Search the web (local-first). Returns a Markdown document: `Source`/`Degraded` header + `Title`/`URL`/`Snippet` blocks (`Last Crawled` per local result). | `query: str`, `num_results=5`, `max_crawl=5`, `max_age_days=None` |
| `fetch_page` | Read one URL as fit markdown (stored-first, else crawl+store). Bumps `fetch_count`. | `url: str`, `max_chars=None` |
| `fetch_pages` | Bulk read under a shared char budget. | `urls: list[str]`, `max_chars=None`, `per_page_chars=None`, `strategy="smart"` |
| `index_stats` | Snapshot of index + gateway (counts, freshness, hit-rate, queue, quota). | — |
| `seed_url` | Queue a URL for background indexing. Returns queue position. | `url: str` |
| `refresh_page` | Force an immediate re-crawl of one page. | `url: str` |

The `search_web` description tells the LLM the contract explicitly: the body
is a Markdown document — `Source`/`Degraded` header, then `Title`/`URL`/
`Snippet` blocks separated by `---` lines — local hits cost zero provider
credits, and a miss triggers background indexing — so the model doesn't need
to wait or re-call.

## Error model

- `400` — invalid/missing body (bad URL, missing `urls`).
- `401` — missing/invalid API key.
- `404` — page/provider not found.
- `502` — search `Source: error` (all providers failed, no local rows) or a
  failed `POST /api/refresh`. The 502 search body is the Markdown header
  (`Source: error` + `Provider Errors:`).

Provider-level failures on `search_web` are *not* HTTP errors — they're
reported in-band via the `Degraded: true` / `Provider Errors:` header lines
so the LLM still gets an answer (degraded local) when possible.
