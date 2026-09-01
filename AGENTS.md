# AGENTS.md

Guidance for coding agents working in this repo. Keep changes consistent with
the existing code and conventions.

## What this is

**wellisearch** is a self-hosted web-search gateway + web-index service for
LLMs. One FastAPI container serves a REST API, an MCP server (Streamable
HTTP), a static dashboard, and a background asyncio worker. Postgres 18 +
`pgvector` is the single store; a native in-process crawler is the single
crawling path. Search is **local-first** (zero provider credits on a hit) with
ordered provider failover on a miss.

## Code style

**All Python must follow [`@CODING_STYLES.md`](CODING_STYLES.md)** — the 15
rules there (parameter layout, file ordering, imports, type hints, docstrings,
settings over magic numbers, lazy `%s` logging, section separators, etc.) are
the contract. Read it before touching `.py` files in `src/`, `tests/`, or
`benchmarks/`. Match the surrounding code's style.

## Stack (locked — don't re-litigate)

- **Python 3.12**, FastAPI, MCP SDK, uvicorn.
- **Postgres 18 + pgvector + pg_trgm** — the only database.
- **fastembed** for embeddings (`all-MiniLM-L6-v2`, 384-dim).
- **Native crawler** (`crawl/` package: patchright + scrapling + curl_cffi) —
  Crawl4AI has been removed; do not reintroduce it.
- **Provider gateway** (`providers/`): `tavily → brave → exa → youcom`,
  ordered failover + a monthly quota ledger.
- One container, one process, one worker task. No queue broker, no object store.

## Where things live

```
src/wellisearch/
  app.py            FastAPI routes, auth middleware, worker startup, MCP mount
  search_web.py     the search pipeline (shared by REST + MCP)
  fetch.py          fetch_page / fetch_pages (stored-first, budgeted)
  tools.py          the six MCP tools
  providers/        gateway: adapters + failover + quota ledger
  crawl/            native crawler: engine, tiers, policy, extractors, lanes
  index.py          store_page: hash → chunk → embed → upsert
  worker.py         background worker (drain queue + watchlist refresh)
  db.py             psycopg pool + all SQL helpers
  config.py         pydantic-settings — every env knob, with defaults
  schema.sql        DDL + fn_search_local (applied at startup)
  static/           dashboard (vanilla JS, no build step)
tests/              see "Tests" below
docs/               reference docs (architecture, search-pipeline, ranking, …)
```

`BLUEPRINT.md` is the approved design; `features.md` is the backlog;
`bugs.md` lists known issues. Reference docs live in `docs/`.

## Invariants & gotchas

- **`EMBED_MODEL` is load-bearing.** Worker and server must use the same model.
  Changing it invalidates all stored vectors — run
  `python -m wellisearch.reindex` (and `--force` to re-embed everything).
- **The search path never blocks on a crawl.** It returns immediately and
  enqueues URLs; the worker drains the queue in the background.
- **REST and MCP share the same pipeline code** (`search_web.py`, `fetch.py`).
  Change the pipeline once, not per surface.
- **Markdown is the default wire format** for `search_web` / `fetch_page` /
  `fetch_pages` (a hard contract — see `docs/api.md`). `format=json` opts into
  the structured envelope.
- **Never log secrets** (API keys, passwords, full request bodies). Use lazy
  `%s`/`%d` formatting in log calls (see CODING_STYLES rule 12).
- **All tunables live in `config.py`** (env-overridable), never as bare
  literals at the call site.

## Tests & verification

Run from the repo root. CI (`.github/workflows/build.yml`) runs the first two.

```bash
python tests/test_units.py       # chunker/truncation/renderers — pure logic (CI)
python tests/test_providers.py   # provider adapters, mocked httpx — pure logic (CI)
python tests/test_crawl_core.py  # crawl core (policy/botwall/signals/engine)
python tests/test_lanes.py       # two-lane crawl routing
python tests/test_extractors.py  # per-site extractors (fixture HTML)
python tests/test_db.py          # schema + fn_search_local + quota (needs Postgres)
python tests/e2e_test.py         # live end-to-end (needs a running container)
python tests/test_tiers_smoke.py # transport tiers (needs the Docker image)
```

The "pure logic" tests need no network, keys, or DB. Verify your change with
the relevant test before reporting it done.

## Config

All knobs are environment variables (see `.env.example` for the annotated
list); defaults live in `config.py`. Full reference: `docs/deployment.md`.
