# Trigram rewrite — why `fn_search_local` changed (2026-08)

**TL;DR:** the trigram leg of `fn_search_local` computed
`similarity()`/`word_similarity()` over **every chunk in the index, on every
search**. At the index's current size (~1.34M chunks, 22.5k pages, ~8.5 GB)
that is a 10–27+ minute CPU-bound scan per query. It exhausted the shared
12-connection Postgres pool, which turned every other database operation —
worker stores, fetches, logging, even `SELECT 1` in `/health` — into a
30-second `PoolTimeout`. Searches became unresponsive, especially while the
worker was crawling (which grows the index and burns CPU). The leg was
rewritten to be **index-bounded**, and a set of hygiene fixes (statement
timeout, crawl cap, thread caps, tick mutex) removed the amplifiers.

---

## 1. Symptoms

- `search_web` / `fetch_page` requests taking **a minute or more** while the
  worker was crawling; sometimes never returning.
- wellisearch container reporting **unhealthy** (its healthcheck runs
  `db.fetch_one("SELECT 1")`, which was timing out on pool acquisition).
- Container logs full of
  `psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec`
  inside the worker (`store_page` → `page_get`), i.e. **crawls failing
  because of the search path's database load**.
- `pg_stat_activity` showing **five concurrent `fn_search_local` queries
  running 8–27 minutes**.
- CPU: wellisearch at 840–1440%, Postgres at ~690% during the episode.

## 2. Diagnosis

The index had grown to **1,337,042 chunks** (~2 GB table + 1.3 GB trigram
GIN + 1.2 GB HNSW + 0.7 GB FTS GIN = ~8.5 GB) with only 256 MB of
`shared_buffers`.

Per-leg measurements of `fn_search_local` on a quiescent system:

| leg | mechanism | cost |
|---|---|---|
| FTS | `tsv @@ tsquery` via `chunks_tsv_gin` | **~1.0 s** |
| Vector | HNSW ordered scan | **~143 ms** |
| Trigram | `GREATEST(similarity, word_similarity, similarity(title)) > 0.05` over **all** chunks | **10–27+ min, never finished in testing** |

So one slow leg accounted for essentially all search latency. Why it is
slow:

1. **No index can serve it.** `word_similarity()` is not indexable at all,
   and wrapping `similarity()` in `GREATEST(...)` defeats the
   `chunks_trgm_gin` GIN index. Every search was a full scan of 1.34M rows,
   computing 2–3 fuzzy-string functions per row.
2. **Even the indexable form is non-selective here.** Tests with a plain
   `text % 'query'` predicate (which the planner *does* route through the
   GIN bitmap) still exceeded 45 s: a common word's trigrams appear in most
   of a homogeneous tech corpus, so the bitmap union covers a huge fraction
   of the table, and Postgres must recheck `similarity()` on every candidate.
   Nonsense words return instantly — cost scales with how much of the corpus
   shares trigrams with the query, which in this corpus is "most of it".
3. **The pool converted latency into outage.** One slow scan holds one of
   only 12 pooled connections for its whole runtime. A handful of concurrent
   searches (or searches + worker stores + dashboard polls) exhausts the
   pool; everything else blocks 30 s on `getconn` and then raises
   `PoolTimeout`. That is the "unresponsive while crawling" state.
4. **Crawling was an amplifier, not a cause** — a death spiral:
   - search enqueues crawls → index grows → trigram scans get bigger;
   - worker embeds (fastembed/ONNX, unbounded threads, 3 parallel) burn
     8–14 cores → the already-slow scans get slower;
   - worker stores hold pool connections → exhaustion sooner.

Answer to "is it the database or the server setup?": **the database query is
the root cause; the shared pool + CPU oversubscription are the amplifiers**
that turned slow searches into a total stall.

## 3. The changes

### 3.1 Core fix — bounded trigram leg (`schema.sql`)

Old leg (per search, over the whole corpus):

```sql
trg AS (
  SELECT c.id, ROW_NUMBER() OVER (ORDER BY
    GREATEST(similarity(c.text, query),
             word_similarity(c.text, query),
             similarity(p.title, query)) DESC)
  FROM chunks c JOIN pages p ON p.url = c.url
  WHERE GREATEST(...) > 0.05          -- full scan, 10-27 min at 1.34M rows
)
```

New leg (candidates from index-backed, corpus-fraction-bounded sets —
re-ranked with the fuzzy functions on that small set only):

```sql
-- query words in < 1% of the corpus (per-word GIN index counts, ms each)
rare AS (
  SELECT string_agg(lexeme, ' | ') AS words
  FROM words
  WHERE (SELECT count(*) FROM chunks c
         WHERE c.tsv @@ to_tsquery('english', words.lexeme)) * 100
        < (SELECT nchunks FROM q)
),
trg AS (
  SELECT cand.cid, cand.url, ROW_NUMBER() OVER (ORDER BY
    GREATEST(similarity(cand.text, query),
             word_similarity(cand.text, query)) DESC)
  FROM (
    SELECT DISTINCT ON (cid) cid, url, text
    FROM (
      SELECT cid, url, text FROM ftsr WHERE rnk <= 50     -- AND set, top 50
      UNION ALL
      (SELECT c.id, c.url, left(c.text, 1000)             -- rare words, top 50
       FROM chunks c
       WHERE c.tsv @@ to_tsquery('english', (SELECT words FROM rare))
       ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', (SELECT words FROM rare))) DESC
       LIMIT 50)
    ) u ORDER BY cid
  ) cand
)
```

Four details matter here, each cost-measured on this corpus:

1. **AND set, not OR set, as the primary candidate pool.** For multi-word
   queries the OR set is tens of thousands of rows (any single word matches;
   70k for "postgres connection pool", 127k for a 5-word query with "use"
   in it — 2.4 s and 6.1 s scans respectively). The AND set is the
   strong-overlap set and is small (190 rows, 176 heap pages, **~13 ms**).
   The OR set is used by the FTS leg only when the AND set is empty (typos,
   long queries) — the same slow path the original bounded design had.
2. **Distinctive-word pool for partial matches.** A page that contains the
   query's rare word ("pgvector": 3,470 chunks) but not every word is
   invisible to the AND set — yet it is often exactly what the user wants.
   Words appearing in < 1% of the corpus are counted with per-word GIN index
   probes (a few ms each); their OR set stays small even inside long
   queries, so its top 50 is cheap. Common words stay out of the pool
   (their OR sets are the multi-second scans above). `tests/test_db.py`
   caught two earlier variants: a `COALESCE(tsq_and, tsq_or)` selection —
   a tsquery is only NULL when the query has *no* lexemes, so empty-AND
   queries silently lost the leg — and an AND-only pool, which ranked a
   stored page matching 2 of 5 query words below 49 real pages.
3. **Prefix the fuzzy functions: `left(text, 1000)`.** `similarity()` cost
   scales with text length — some chunks run to ~1 MB — and re-ranking 190
   full chunks took 637 ms vs **11 ms** over `left(text,1000)`. The head of a
   chunk is the most representative part anyway, and the candidate `text`
   column is truncated at the source so the full text is never transported
   out of the index scan.
4. **`ORDER BY`/`LIMIT` inside parentheses after `UNION ALL`** bind to the
   branch, not the whole union (unparenthesized they apply to the union and
   reference a table alias that is out of scope — a parse error).

Cost is now: one or two small GIN bitmap fetches + a handful of per-word
index counts + fuzzy scoring on ≤100 short strings. Measured end-to-end on
this corpus: **8–111 ms** for the FTS/trigram legs across query shapes
(vs 10–27+ min for the old leg).

### 3.1b Vector leg — `LIMIT 50` keeps HNSW early-stop (`schema.sql`)

The original `vec` CTE had no LIMIT, so Postgres could not early-stop the
HNSW scan: it did a **full seq scan + external sort of all ~1.4M embeddings**
(~7–9 s, spilling to disk) on every search. The fix is the canonical pgvector
shape — inner `ORDER BY c.embedding <=> qvec LIMIT 50`, outer
`ROW_NUMBER()`:

```sql
vec AS (
  SELECT t.cid, t.url, ROW_NUMBER() OVER (ORDER BY t.d) AS rnk
  FROM (
    SELECT c.id AS cid, c.url AS url, c.embedding <=> qvec AS d
    FROM chunks c
    WHERE c.embedding IS NOT NULL AND qvec IS NOT NULL
    ORDER BY c.embedding <=> qvec
    LIMIT 50
  ) t
)
```

Measured: `Index Scan using chunks_vec_hnsw`, **2–250 ms** vs 7–9 s full
sort. (Caveat: if a future query shape inlines the CTE and drops the LIMIT
from the ordered subquery, the planner will fall back to the full sort —
keep the two-stage form.)

### 3.1c Fusion — `url` rides through the legs, no join back to `chunks`

The original `per`/`perpage`/`pagescore` CTEs joined the small leg set
(≤150 rows) back to the 1.4M-row `chunks` table for `url` and snippet text.
The planner answered that with a **hash table over all 1.4M chunks (~16 s)**
per search. Now every leg CTE carries `c.url` alongside `cid`, the fusion
(`per` → `perpage` → `pagescore`) is pure in-memory over ≤150 rows, and only
`bestchunk` touches `chunks` again — a nested loop on the PK fetching snippet
text for the ~k winning pages.

### 3.2 Backstop — statement timeout on search SQL

- `config.py`: `SEARCH_STATEMENT_TIMEOUT_MS` (default **15 000**).
- `db.py`: `fetch_all(..., timeout_ms=...)` applies it via `SET LOCAL
  statement_timeout` inside one explicit transaction — scoped to that
  statement, never leaks into the pool.
- `search_web.py`: the `fn_search_local` call uses it; on
  `QueryCanceled` the pipeline falls through to the provider gateway
  (existing miss path) instead of stalling the request.

15 s is ~10× the healthy 1–2 s runtime, so a regression degrades to a
gateway hit rather than a multi-minute stall.

### 3.3 Hygiene fixes

| change | file | why |
|---|---|---|
| `chunk_markdown` moved to `asyncio.to_thread` | `index.py` | pure-Python CPU work was running on the event loop, stalling **all** request handlers while a page stored |
| worker tick mutex | `worker.py` | interval timer + debounced kick could run two ticks concurrently, doubling crawl/embed load |
| **global** crawl semaphore (`CRAWL_MAX_PARALLEL`, shared by worker *and* fetch paths; dedup waiters don't hold a slot) | `crawler.py`, `queue.py` | `fetch_pages` fanned out unbounded crawls per request; now total concurrent crawls ≤ 3 (matches the crawler's pool size) |
| fastembed `threads=EMBED_THREADS` (default **2**) | `embed.py`, `config.py` | each embed session spawned an unbounded ORT thread pool; 3–4 concurrent sessions oversubscribed the host and starved Postgres |
| `shared_buffers` 256 MB → **2 GB**, `effective_cache_size` 1 GB → **4 GB** | `infra/docker-compose.yml` | the 8.5 GB index was being served through a 256 MB buffer cache — index-backed scans read most pages from disk (container limit 8 G) |

## 4. Tradeoffs

**What the trigram leg no longer does:**

- **Pure-substring candidate discovery.** Query "postgres" no longer pulls in
  chunks containing only "postgresql" via the trigram leg (that chunk is not
  in the FTS-OR candidate set). Mitigation: the **vector leg covers this** —
  MiniLM embeddings of "postgres" and "postgresql" are near-identical — and
  FTS stemming already handles pool/pooling. We traded a capability for
  predictable cost; the semantic leg is the better home for it.
- **Title-similarity signal** (`similarity(p.title, query)`) is gone from
  the leg. Titles rarely add signal beyond the body; the vector leg also
  ranks pages semantically.
- **Leg overlap.** The trigram pool's AND half (top 50) is the same set the
  FTS leg ranks (top 50 by density), so AND-set chunks can receive two RRF
  votes instead of the trigram leg contributing fully independent
  candidates. The rare-word half and the vector leg are independent
  sources. Net effect: word-overlap evidence is weighted a bit more
  heavily. Acceptable — it matches the intent ("topical evidence beats
  marginal matches"), and the vector leg still supplies independent
  semantic candidates.
 - **Prefix re-ranking** only fuzzy-scores the first 1000 chars of a chunk;
   a long chunk whose query words appear only late gets less trigram
   credit. FTS density (full-text `ts_rank_cd`) and the vector leg still see
   the whole chunk, so this is a re-ranking nuance, not a recall change.
  - **`SEARCH_MIN_SCORE` was recalibrated 0.12 → 0.06** as part of this work:
    the bounded leg produces a lower band (top on-topic 0.070–0.130, off-topic
    tops ≤ 0.045; see `ranking.md`), and 0.12 sat above it —
    silently routing good local hits to the provider gateway. (Since
    superseded: the local-vs-gateway decision now uses the `coverage` gate —
    `ranking.md` § Local-hit gate.)

**Other tradeoffs:**

- **Statement timeout** turns an (old, broken) slow-but-eventual local answer
  into a fast gateway hit — i.e., a rare case where the user *pays provider
  credits* instead of waiting. That is the intended direction: latency is
  bounded, cost is occasional.
- **Global crawl cap**: a `fetch_page` for an unknown URL may now wait
  behind worker crawls for up to ~45 s (crawl timeout) to acquire a slot.
   Alternative was unbounded fan-out that saturates the crawler and the pool —
  the current behavior is the safer failure mode.
- **EMBED_THREADS=2**: per-batch embedding is marginally slower on a
  many-core server than with all cores; on this host it is the difference
  between "fast but starves Postgres" and "slightly slower, everything
  responsive". Tune via env if the box changes.
- **`chunks_trgm_gin` is now unused** (~1.3 GB, and it adds write
  amplification to every chunk insert). It was deliberately **kept**: it is
  the rollback path for the old trigram behavior. Dropping it
  (`DROP INDEX chunks_trgm_gin`) reclaims space and speeds up stores — do it
  if you're confident the new leg is good.

## 5. Verification (2026-08-24, post-change)

End-to-end `fn_search_local(..., 10)` on the ~1.34M-chunk index, all
query shapes:

| query shape | measured | path |
|---|---|---|
| 3 common words ("postgres connection pool") | **111 ms** | AND set (192 rows, ~13 ms) + rare pool (empty) |
| 5 words, 1 rare word ("how to use pgvector for semantic search") | **54 ms** | AND set (71 rows) + rare-OR ("pgvector", 3,470 rows) top 50 |
| Off-topic ("best pizza nyc") | **8 ms** | tiny AND set, low scores |
| AND-empty (typos / long queries) | 2.4–6 s fallback | full OR set — the one slow path, same as the original bounded design |
| Vector leg | 2–250 ms | `chunks_vec_hnsw` Index Scan (early-stopping) |

(All vs **10–27+ min** for the pre-rewrite leg; the full-corpus trigram
scan and the full-embedding sort are gone from every path.)

Scoring with real query embeddings (not a page's stored vector — that
corrupts the probe): top on-topic 0.070–0.130, off-topic tops ≤ 0.045 →
`SEARCH_MIN_SCORE` set to 0.06, so on-topic queries serve local (zero
provider credits) and off-topic ones route to the gateway. (This score gate
was later found to cross clusters — off-topic "autumn" 0.134 vs on-topic
"docker" 0.049 — and replaced by the `coverage` gate; `ranking.md` §
Local-hit gate.)

Test suite on the rebuilt image, live ~1.34M-chunk index:

- `tests/test_units.py` — **ALL UNIT TESTS PASSED**
- `tests/test_db.py` — **ALL DB INTEGRATION TESTS PASSED** (schema apply,
  `store_page` unchanged short-circuit, `fn_search_local` finds a freshly
  stored pgvector page — ranked ~49th among a corpus full of real pgvector
  pages, asserted top-100 findability — queue dedupe, quota ledger,
  provider state, event_log)
- `tests/e2e_test.py` against the running server — **39 passed, 0 failed**,
  including `search_web` returning `Source: local` and `fetch_page`
  serving from the index.

Live `GET /api/search` during an active worker crawl: 1.5–2.5 s total
(embedding + SQL + HTTP); `pg_stat_activity` shows no multi-minute
`fn_search_local` rows; no `PoolTimeout` in worker logs; container
`healthy`.

## 6. Follow-ups (open)

1. **Drop `chunks_trgm_gin`** if the new leg proves out (1.3 GB + write
   amplification).
2. **Index growth is the real long-term threat**: this episode was triggered
   by the index reaching ~22.5k pages (and it was adding ~2k pages/hour
   during heavy crawling). There is no pruning of low-value pages today
   (`pages.disabled` exists but nothing sets it automatically). Consider a
   retention policy for pages with `fetch_count = 0` and old `last_crawled`.
3. Consider a **dedicated executor** for embedding (`to_thread`'s default
   pool is shared with chunking) if worker throughput ever becomes a
   bottleneck.
4. Re-measure the local-hit gate (`LOCAL_MIN_COVERAGE`) after any big
   index-size change (`ranking.md` → "Local-hit gate").
