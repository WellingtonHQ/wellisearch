# Search pipeline (`search_web`)

`src/wellisearch/search_web.py` is the single implementation behind both
`GET /api/search` and the MCP `search_web` tool. There is no crawl in the
response path — searches are always served from the local index or from a
provider, and indexing happens in the background.

```
images/search-pipeline.svg
```

![search pipeline](images/search-pipeline.svg)

## Steps

### 1. Resolve defaults

```python
k       = max(1, num_results or SEARCH_K)  # default 5; clamped so negative k can't slice rows off the end
crawl_n = SEARCH_MAX_CRAWL if max_crawl is None else max(0, max_crawl)  # default 5
```

> **`search_mode`** selects the source (default `auto`): `auto` — the default
> flow below; `local` — steps 2–4 only, no provider; `provider` — bypasses
> steps 2–4, the local index is not touched. Full semantics (degraded
> fallback, empty index → `source: error`) are in
> [api.md](api.md#get-apisearch).

### 2. Embed the query

`embed_one(query)` runs fastembed (`EMBED_MODEL`, 384-d) on a worker thread
(`asyncio.to_thread`). If it fails, the pipeline **degrades gracefully**:
`qvec` is passed as `NULL` and the vector leg of `fn_search_local` is simply
skipped — FTS + trigram still rank.

### 3. Rank the local index

```sql
SELECT * FROM fn_search_local(%s, %s::vector, %s)
```

See [ranking.md](ranking.md) for the full algorithm. Returns at most
`max(k, 10)` rows with `url, title, snippet, score, coverage, last_crawled,
fetch_count` (the extra rows let the gate below see past the top-k by
score). If the function itself errors, `local_rows = []` **and** the failure
is returned as `index_error`: auto mode continues to the gateway (the error
stays hidden behind the fallback), local mode surfaces it in the error
envelope (`index_error` field in JSON, `Index Error:` header line in
Markdown) so "the index is empty" and "the index is down" are
distinguishable.

**Optional freshness filter**: if `max_age_days` is given, rows with
`last_crawled` older than the cutoff are dropped (rows never crawled are
kept). This is applied *after* ranking, in Python.

### 4. Gate: is the local result good enough?

```python
serve_local = any((r.get("coverage") or 0) >= LOCAL_MIN_COVERAGE for r in local_rows)
```

`LOCAL_MIN_COVERAGE` defaults to **0.75**. `coverage` (computed in
`fn_search_local`, entirely in Postgres) is the fraction of the query's
content words the page's title+body contains — the actual "do we have this
answer?" signal. The RRF `score` is rank-only and does not gate (off-topic
pages can outscore on-topic ones: 0.134 vs 0.049 on this index), nor does
cosine similarity (0.410 on-topic vs 0.503 off-topic). See
[ranking.md § Local-hit gate](ranking.md#local-hit-gate-coverage) for the
calibration data.

### 5a. Local hit → serve (zero provider credits)

- `source = "local"`, `degraded = false`
- results truncated to `k`, each with the snippet capped at 400 chars
- every served page gets `search_hit_count += 1` — one batched
  `mark_search_hits(urls)` UPDATE for all results
  (the hit-rate signal for the dashboard; the prominence counter
  `fetch_count` is bumped separately by the read path — `fetch_page` /
  `fetch_pages`)

### 5b. No good local hit → provider gateway

`gateway.search(query, k)` tries the providers one by one, in the order
currently set (a dashboard/API override via `PUT /api/providers/order` when
set, else the `SEARCH_PROVIDERS` default). A provider serves iff it passes
all gates:

1. **enabled** — `provider_state.enabled` (runtime toggle, default true)
2. **configured** — API key / base URL present in env
3. **quota** — `provider_quota.used < limit` for the current month
    (limit = runtime `limit_override` or env default `TAVILY_QUOTA_MONTHLY` /
    `BRAVE_QUOTA_MONTHLY` / `EXA_QUOTA_MONTHLY` / `YOUCOM_QUOTA_MONTHLY`; `None` = unlimited)
4. **non-empty result set** — a 200 with zero results is a *soft failure*
   and failover continues

Any failure is recorded in `provider_state.last_error` (visible in
`GET /api/providers` and the dashboard) and appended to the error chain.
On success: `quota_bump(provider)` (increments the monthly ledger),
`last_served = now()`, `last_error = NULL`.

If **every** provider fails, the gateway raises `GatewayExhausted(errors)`.

### 6. Degraded mode and hard failure

- `GatewayExhausted` + local rows exist → **degraded mode**: serve the local
  rows anyway, with `source = "local"`, `degraded = true`, and
  `provider_errors` attached. (BLUEPRINT §14.12: better stale than nothing.)
- `GatewayExhausted` + no local rows → `source = "error"`, empty results.
  The REST layer maps this to **HTTP 502**; MCP returns the dict with
  `provider_errors`.

### 7. Speculative indexing (gateway path only)

The top `crawl_n` result URLs are enqueued:

```python
await queue.enqueue(r.url, source="search")   # + debounced kick
```

This is the cache-warming loop: the pages the provider just found are
crawled, chunked, and embedded in the background, so the *next* query for
the same topic is a free local hit. Enqueue is deduped (partial unique index
on `url` for `pending`/`in_flight`), so repeated misses don't pile up work.

### 8. Log + respond

Every request — local, gateway, degraded, error — is logged:

```sql
INSERT INTO search_log (query, source, local_hits, results)
```

`local_hits` is the count of *good* local rows (or all local rows in
degraded mode), so the dashboard can compute local-hit rate over time.

## Response contract

By default the response is a **plain Markdown document** (no JSON envelope) —
REST serves it as `text/markdown`, the MCP tool returns it as a text block.
Both surfaces render the same internal envelope via
`search_web.render_search_markdown()`. Set `format=json` (REST) or
`format="json"` (MCP) — or send `Accept: application/json` over REST — to get
the structured JSON envelope instead (the internal dict, not this Markdown).

```
Source: local
Degraded: false
Time: 1234 ms (index: 45 ms)

Title: PostgreSQL 18 Documentation
URL: https://www.postgresql.org/docs/current/
Last Crawled: 2026-08-20T14:03:11Z
Snippet: The PostgreSQL documentation ...
---
Title: FastAPI MCP server
URL: https://...
Snippet: ...
```

Header lines (response-level):

| Line | Notes |
|---|---|
| `Source:` | `local` \| `tavily` \| `brave` \| `exa` \| `youcom` \| `error` |
| `Degraded:` | `true` \| `false` — true only in §6 degraded mode |
| `Time:` | total ms, split into `index:` (Postgres search) and — only when a provider was used — `provider:` (gateway wait); in `search_mode=provider` the `index:` part is omitted (the index leg never ran) |
| `Provider Errors:` | `{provider}: {error}` pairs joined by `;` — present only when providers failed |
| `Index Error:` | the index-leg failure (exception text) — present only when `search_mode=local` and the index leg itself failed (mirrors `provider_errors`; the `index_error` JSON field) |

Result blocks:

| Line | Notes |
|---|---|
| `Title:` | page title (falls back to the URL) |
| `URL:` | result URL |
| `Last Crawled:` | ISO timestamp; local hits only, omitted when the page was never crawled |
| `Snippet:` | ~400-char chunk text |

`source = error` (nothing to serve): header only, no blocks.

## Behavioral guarantees

- **Deterministic order** — results come from one ranked SQL function; REST
  and MCP can never disagree.
- **No blocking crawl** — worst-case latency is one embedding + one SQL
  query + (on a miss) one provider call.
- **Quota-safe** — local hits never touch the ledger; a provider that is
  exhausted is skipped before any HTTP call.
- **Self-healing index** — every gateway answer seeds the index for next
  time; every `fetch_page` read of an unindexed URL stores it.
