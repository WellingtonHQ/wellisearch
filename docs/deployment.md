# Deployment & operations

## Image

`Dockerfile` builds a slim, self-contained image:

- Base: `python:3.12-slim`.
- Installs the package (`pip install .`) — the app is a normal Python package
  under `src/`.
- **Pre-warms the embedding model** into `FASTEMBED_CACHE_DIR=/opt/fastembed`
  at build time (`sentence-transformers/all-MiniLM-L6-v2`), so the first
  request doesn't pay the ~90 MB model download.
- `EXPOSE 8780`, a `HEALTHCHECK` on `/health`, and a `CMD` that runs
  `uvicorn wellisearch.app:app --host 0.0.0.0 --port 8780`.

## Compose

`compose.yml` defines one service, `wellisearch`, with:

- `env_file: .env` (all config comes from the environment).
- `ports: 8780:8780`.
- `restart: unless-stopped`.
- a `healthcheck` (Python `urllib` probe of `/health`, 60 s start period).
- **two external networks**:
  - `wellington_default` — the OWUI/agent stack network: reach `crawl4ai`
    and `searxng` by service name, and be reachable as
    `wellisearch:8780` for the MCP SSE endpoint.
  - `postgres-net` — the infra project network: reach the shared Postgres
    container by the `postgres` alias.

Both networks are **external** (owned by their projects). There is
deliberately **no `depends_on` on Postgres** — cross-project startup ordering
is handled by the app's own DB retry (below).

```
docker compose -f compose.yml build
docker compose -f compose.yml up -d
docker compose -f compose.yml ps            # health: healthy
docker compose -f compose.yml logs -f wellisearch
```

## Startup sequence (no depends_on)

`db.startup` (§11, cross-project) does, in order:

1. **Retry-connect** to the admin DB (`POSTGRES_ADMIN_DB`, default `shared`)
   — up to `STARTUP_RETRIES` (10) × `STARTUP_RETRY_S` (3 s).
2. **Idempotently `CREATE DATABASE`** the app DB (`POSTGRES_DB`, default
   `wellisearch`) if it doesn't exist. The identifier is quote-escaped.
3. **Open the connection pool** (min 2 / max 12) against the app DB,
   registering the pgvector type adapter on every connection.
4. **Apply `schema.sql`** (idempotent DDL + `fn_search_local`).

Then `app.py:_startup` resets stuck `in_flight` queue rows and starts the
worker task. This is why wellisearch can start before Postgres is ready and
still come up cleanly.

## Configuration reference

All knobs are environment variables read by `config.py` (pydantic-settings).
`.env` supplies them; defaults shown are from `config.py`.

### Postgres
| Var | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` | `postgres` | network alias of the shared infra container |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_USER` | `wellington` | |
| `POSTGRES_PASSWORD` | `change-me` | **set this** |
| `POSTGRES_DB` | `wellisearch` | app DB (self-created) |
| `POSTGRES_ADMIN_DB` | `shared` | maintenance DB used only to self-create the app DB |

### Crawl4AI
| Var | Default | Notes |
|---|---|---|
| `CRAWL4AI_URL` | `http://crawl4ai:11235` | the `/md` endpoint base |
| `CRAWL4AI_API_KEY` | *(empty)* | Bearer key |

### Search providers
| Var | Default | Notes |
|---|---|---|
| `SEARCH_PROVIDERS` | `tavily,brave,searxng` | ordered; first non-empty success serves |
| `TAVILY_API_KEY` | *(empty)* | |
| `TAVILY_QUOTA_MONTHLY` | `1000` | `0`/unset = unknown; still fails over on 429 |
| `BRAVE_API_KEY` | *(empty)* | |
| `BRAVE_QUOTA_MONTHLY` | `1000` | |
| `SEARXNG_URL` | `http://searxng:8080` | keyless last-resort (JSON format on) |
| `PROVIDER_TIMEOUT_S` | `20` | per-provider HTTP timeout |

### Embeddings (load-bearing)
| Var | Default | Notes |
|---|---|---|
| `EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | worker + server **must** match; changing it invalidates all vectors |
| `EMBED_DIMS` | `384` | guarded at load time against the model's real size |

### Search
| Var | Default | Notes |
|---|---|---|
| `SEARCH_K` | `5` | default result count |
| `SEARCH_MAX_CRAWL` | `5` | gateway result URLs to pre-index per miss |
| `SEARCH_MIN_SCORE` | `0.12` | local-hit threshold (see ranking.md) |
| `STALE_HOURS` | `72` | staleness hint for stats/dashboard |
| `MAX_CHUNK_TOKENS` | `800` | chunk budget (~4 chars/token) |

### Fetch truncation
| Var | Default | Notes |
|---|---|---|
| `FETCH_DEFAULT_STRATEGY` | `smart` | `smart\|head\|tail\|even\|priority` |
| `FETCH_MAX_CHARS` | `40000` | total budget when `max_chars` omitted (`0` = unlimited) |
| `FETCH_PER_PAGE_CHARS` | `12000` | per-page cap |

### Worker / queue
| Var | Default | Notes |
|---|---|---|
| `WORKER_INTERVAL_MIN` | `30` | periodic tick period |
| `WORKER_BUDGET_PER_RUN` | `25` | URLs per drain / per watchlist pass |
| `WORKER_TICK_BUDGET_MIN` | `15` | wall-clock budget per tick |
| `KICK_DEBOUNCE_S` | `5` | coalesce burst enqueues into one tick |
| `QUEUE_MAX_ATTEMPTS` | `3` | retries before a queue row is `failed` |
| `CRAWL_TIMEOUT_S` | `45` | Crawl4AI per-URL timeout |
| `CRAWL_MAX_PARALLEL` | `3` | concurrent crawls |

### Server
| Var | Default | Notes |
|---|---|---|
| `BIND_PORT` | `8780` | |
| `WELLISEARCH_API_KEY` | *(empty)* | empty = open; set = require on REST + MCP |

## Operations

### Health & status
```
curl http://localhost:8780/health
curl -H "Authorization: Bearer $KEY" http://localhost:8780/api/stats
```
`/health` probes the DB, Crawl4AI, and each provider (configured + state).

### Manual worker run (drain now)
```
docker compose -f compose.yml exec wellisearch python -m wellisearch.worker --once
```
Resets stuck `in_flight` rows, then runs one tick (drain queue + watchlist
refresh) and exits.

### Re-embed after an embedding-model change
```
python -m wellisearch.reindex            # re-embed pages with a stale model
python -m wellisearch.reindex --force    # re-embed every page
python -m wellisearch.reindex --dry-run  # report only
```
Pages already on the current model with unchanged content are a no-op
(`store_page`'s short-circuit).

### Provider runtime controls
```
# disable a provider at runtime (persists in provider_state)
curl -X PATCH -H "Authorization: Bearer $KEY" \
     -d '{"enabled": false}' http://localhost:8780/api/providers/tavily

# override its monthly limit
curl -X PATCH -H "Authorization: Bearer $KEY" \
     -d '{"limit": 200}' http://localhost:8780/api/providers/brave

# inspect the ledger + state
curl -H "Authorization: Bearer $KEY" http://localhost:8780/api/providers
```

### Re-ranking (changing `fn_search_local`)
1. Edit `src/wellisearch/schema.sql`.
2. Rebuild + recreate (schema re-applies at startup):
   `docker compose -f compose.yml build && docker compose -f compose.yml up -d`.
3. Verify with `tests/test_db.py` (exercises `fn_search_local`) and the e2e
   suite.

## Running on a host (non-Docker)

- `pip install .` then `python -m wellisearch.app` (or uvicorn directly).
- On **Windows**, `app.py:main` forces the selector event loop
  (`wellisearch.loopfix:loop_factory`) because uvicorn 0.36+ defaults to the
  proactor loop, which psycopg's async driver needs the selector loop for.
  `db.py` also sets `WindowsSelectorEventLoopPolicy` early as a backstop.
- The shared Postgres / Crawl4AI / SearXNG hosts must be reachable from the
  host (adjust `CRAWL4AI_URL`, `SEARXNG_URL`, `POSTGRES_HOST` accordingly).

## Security notes

- Set `WELLISEARCH_API_KEY` for any network the container is reachable on.
  It gates `/api/*` and `/mcp/*`; the dashboard and `/health` stay open.
- Keep provider API keys and the Postgres password only in `.env` (gitignored).
  `.env.example` holds placeholders.
- The API-key comparison is constant-time; the MCP SSE endpoint enforces the
  same auth via the shared middleware.

## Testing

- `tests/test_units.py` — pure-logic tests (chunker, truncation, gateway
  failover) with no network/DB. Fast, always run.
- `tests/test_db.py` — schema + `fn_search_local` + quota/state helpers
  against a real Postgres.
- `tests/e2e_test.py` — live end-to-end against the running container
  (search, fetch, provider failover, degraded mode).
