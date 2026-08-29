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
| `max_age_days` | float | unset | drop local rows crawled older than this (never-crawled kept); ignored with `search_mode=provider` (the index is never consulted) |
| `search_mode` | string | `auto` | `auto` (local first, provider on a miss), `local` (index only — an error if the index has nothing), or `provider` (bypass the local index and force a live provider answer) |
| `format` | string | `markdown` | `markdown` or `json`; wins over the `Accept` header |

By default returns the Markdown search document (`Content-Type:
text/markdown`, no JSON envelope — see [search-pipeline.md](search-pipeline.md)):
a `Source:` / `Degraded:` / `Time:` header (+ `Provider Errors:` when providers
failed), then `Title:` / `URL:` / `Snippet:` blocks separated by `---` lines;
local hits carry a `Last Crawled:` line per result. The `Time:` line shows the
total ms split into `index:` (Postgres index search) and — only when a provider
was used — `provider:` (gateway wait). `Source: error` maps to **HTTP 502**.
Set `format=json` (or send `Accept: application/json`) for the structured JSON
envelope instead (`Content-Type: application/json`):
`{ source, degraded, count, timing: { total_ms, index_ms?, provider_ms? },
results: [{ url, title, snippet, score, last_crawled?, fetch_count? }],
provider_errors?, index_error? }`. With `search_mode=provider` the `index_ms`
key is absent (the index leg never runs) and `provider_ms` is always present;
with `search_mode=local` there is no `provider_ms`, and a failed index leg
(only visible in this mode — auto mode falls back to the provider) adds
`index_error` to the envelope and an `Index Error:` line to the Markdown
header. An invalid `format` or `search_mode` is a **400**.

### `POST /api/fetch`
Read one URL as fit markdown (stored-first, else crawl+store).

Body: `{ "url": "...", "max_chars": 12000, "format": "markdown" }`
(`max_chars` and `format` optional).

By default returns the Markdown page document (`Content-Type: text/markdown`,
no JSON envelope): a `Title:` / `URL:` / `From Index:` / `Chars:` / `Truncated:`
header plus a `Time:` line, then the page body. The `Time:` line shows the
total ms split into `index:` (Postgres lookup) and — only when the page had to
be crawled — `crawl:` (crawl4ai round-trip). A failed fetch returns a `URL:` /
`Status: failed` / `Error:` header (HTTP 200). Bumps the page's `fetch_count`.
Set `format=json` (or send `Accept: application/json`) for the structured JSON
envelope instead (`Content-Type: application/json`):
`{ ok, url, title, markdown, chars, truncated, from_index,
timing: { total_ms, index_ms, crawl_ms? } }` (a failed fetch is
`{ ok: false, url, error, timing: { total_ms } }`). An invalid `format` is a
**400**.

### `POST /api/fetch-bulk`
Bulk read under a shared character budget.

Body:
```json
{
  "urls": ["https://…", "https://…"],
  "max_chars": 40000,
  "per_page_chars": 12000,
  "strategy": "smart",
  "format": "markdown"
}
```
`max_chars`/`per_page_chars` omitted → server defaults; explicit `0`/`null` →
unlimited. `strategy` ∈ `smart | head | tail | even | priority`. `format`
(`markdown` default) wins over the `Accept` header.

By default returns the combined Markdown document (`Content-Type:
text/markdown`, no JSON envelope): a `Strategy:` / `Budget:` / `Pages Fetched:` /
`Total Chars:` / `Truncated:` header (`Budget:` only when a budget is set) plus
a `Time:` line (total ms split into `index:` and — when any page was crawled —
`crawl:`), then one `Title:` / `URL:` / `From Index:` / `Chars:` / `Truncated:`
section per page (body after a `---` line, trimmed pages keep their
`[truncated — N chars omitted, strategy=X]` marker); failed URLs get a
`URL:` / `Status: failed` / `Error:` section. Nothing fetched → the header
carries `Pages Fetched: 0` / `Status: failed` / `Error:` plus one error
section per URL (HTTP 200). Set `format=json` (or send `Accept: application/json`)
for the structured JSON envelope instead (`Content-Type: application/json`):
`{ ok, pages_fetched, truncated, total_chars, strategy, budget?,
timing: { total_ms, index_ms, crawl_ms? }, pages: [{ url, title, content, chars,
truncated, omitted, from_index }] }` (failed/bad URLs in `pages` carry
`{ url, error }`). An invalid `format` is a **400**.

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

### `GET /owui/openapi.json`
Curated OpenAPI 3.0 spec for OWUI's OpenAPI tool server: only the three
user-facing tools (`search_web`, `fetch_page`, `fetch_pages`) with clean
operationIds — OWUI never sees the admin endpoints. Served unauthenticated
(public API contract; the endpoints themselves stay auth-gated). The spec
lives in `src/wellisearch/owui/openapi.json`.

### `GET /`
Serves `static/index.html` (the dashboard). Mounted last (catch-all) so it
never shadows `/api/*` or `/mcp/*`.

## MCP tools (6)

One transport, one tool surface (auth-gated, mounted under `/mcp`):

- **Streamable HTTP (stateless)** — `POST /mcp/http` (JSON-RPC; responses
  ride an SSE stream). One request per call, no server-side session map, so
  a server restart never strands a client. Connect an MCP client to
  `http://localhost:8780/mcp/http`.

All six tools call the same code as their REST counterparts.

| Tool | Purpose | Params |
|---|---|---|
| `search_web` | Search the web (local-first). Returns a Markdown document by default: `Source`/`Degraded`/`Time` header + `Title`/`URL`/`Snippet` blocks (`Last Crawled` per local result). `search_mode` chooses the source: `auto` (default), `local` (index only), `provider` (bypass the index, force a provider). `format="json"` returns the JSON envelope (with a `timing` object). | `query: str`, `num_results=5`, `max_crawl=5`, `max_age_days=None`, `search_mode="auto"`, `format="markdown"` |
| `fetch_page` | Read one URL as fit markdown (stored-first, else crawl+store). Returns a Markdown document by default: `Title`/`URL`/`From Index`/`Chars`/`Truncated` header + `Time` line + page body. `format="json"` returns the JSON envelope (with a `timing` object). Bumps `fetch_count`. | `url: str`, `max_chars=None`, `format="markdown"` |
| `fetch_pages` | Bulk read under a shared char budget. Returns a Markdown document by default: `Strategy`/`Budget`/`Pages Fetched`/`Total Chars`/`Truncated` header + `Time` line + one section per page. `format="json"` returns the JSON envelope (with a `timing` object). | `urls: list[str]`, `max_chars=None`, `per_page_chars=None`, `strategy="smart"`, `format="markdown"` |
| `index_stats` | Snapshot of index + gateway (counts, freshness, hit-rate, queue, quota). | — |
| `seed_url` | Queue a URL for background indexing. Returns queue position. | `url: str` |
| `refresh_page` | Force an immediate re-crawl of one page. | `url: str` |

The tool descriptions tell the LLM the contract explicitly: each of
`search_web`, `fetch_page`, and `fetch_pages` returns a Markdown document by
default (`Source`/`Degraded`/`Time` header + `Title`/`URL`/`Snippet` blocks for
search; `Title`/`URL`/`From Index`/`Chars`/`Truncated` header + `Time` line for
fetch), or the structured JSON envelope when `format="json"` (which carries a
`timing` object: `total_ms`, `index_ms`, and `provider_ms`/`crawl_ms` when that
leg ran) — local search hits cost zero provider credits, and a miss triggers
background indexing, so the model doesn't need to wait or re-call.

## Error model

- `400` — invalid/missing body (bad URL, missing `urls`, or an invalid
  `format` value).
- `401` — missing/invalid API key.
- `404` — page/provider not found.
- `502` — search `Source: error` (all providers failed, no local rows — or,
  in `search_mode=local`, the index leg itself failed) or a failed
  `POST /api/refresh`. The 502 search body is the `source: error` envelope
  (Markdown header with `Provider Errors:` / `Index Error:` where
  applicable, or the JSON envelope with `provider_errors` / `index_error`
  when `format=json`).

Provider-level failures on `search_web` are *not* HTTP errors — they're
reported in-band via the `Degraded: true` / `Provider Errors:` header lines
so the LLM still gets an answer (degraded local) when possible.
