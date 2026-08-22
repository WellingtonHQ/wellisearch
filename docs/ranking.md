# Ranking (`fn_search_local`)

The local ranking core is a **single Postgres SQL function** —
`fn_search_local(query text, qvec vector(384), k int)` — defined in
`src/wellisearch/schema.sql` and re-applied idempotently at every startup
(`db.startup`). Ranking in the database (not in Python) guarantees the REST
API, the MCP server, and any future client all see identical results.

```
images/ranking.svg
```

![ranking](images/ranking.svg)

## The three legs

Each leg returns the **top 50 chunks** (by its own relevance order) for the
query. Chunks are the unit of ranking; pages are then built from their
chunks.

### Leg 1 — Full-text (tsvector)

- `chunks.tsv` is a generated column:
  `to_tsvector('english', text)` (GIN-indexed, `chunks_tsv_gin`).
- The query is parsed with `plainto_tsquery('english', query)`, which yields
  an **AND** of all lexemes.
- **OR fallback**: if *no* chunk contains every lexeme (normal for
  multi-word queries — the words rarely co-occur in one 800-token chunk),
  the *same lexemes* are re-parsed with `&`→`|` and matched with OR. The
  fallback only runs when the AND leg returns zero rows
  (`NOT EXISTS (SELECT 1 FROM fts_and)`), so a strong exact-phrase page is
  never diluted by weak partial matches.
- Ranking within the leg: `ts_rank_cd(c.tsv, query)` (cover-density
  ranking), `ROW_NUMBER()` ordered by rank desc.

### Leg 2 — Trigram (pg_trgm)

- For each chunk, the score is
  `GREATEST(similarity(c.text, query), word_similarity(c.text, query),
  similarity(p.title, query))` — chunk body **and** page title.
- Chunks scoring `> 0.05` qualify; ranked by that score desc.
- GIN-backed: `chunks_trgm_gin` (`gin_trgm_ops`).
- Purpose: catches typos, fragments, and word-order variations that FTS
  tokenization misses — the errors LLMs actually emit.

### Leg 3 — Vector (pgvector)

- `c.embedding <=> qvec` (cosine distance; HNSW index
  `chunks_vec_hnsw` on `vector_cosine_ops`).
- Chunks with a NULL embedding or a NULL `qvec` are excluded — so if query
  embedding fails, the function degrades to FTS + trigram automatically.
- All embeddings are `EMBED_DIMS` = 384-d from
  `EMBED_MODEL` = `sentence-transformers/all-MiniLM-L6-v2` (fastembed).

## Fusion — Reciprocal Rank Fusion with a top-3 cap

Naive RRF over all top-50 chunks has a failure mode: **volume beats
precision**. A page with 50 marginal matches (ranks 15–50 in several legs)
accumulates more RRF mass than a page with 3 excellent matches. This is
exactly what happened when a marketing-heavy page with dozens of loosely
related chunks outranked the authoritative documentation page for
"postgresql documentation reference".

The fix is the **per-page, per-leg top-3 cap**:

```sql
per      AS (SELECT leg, cid, rnk,
             ROW_NUMBER() OVER (PARTITION BY leg, c.url ORDER BY rnk) AS rn_in_page
             FROM leg JOIN chunks c ON c.id = leg.cid),
perpage  AS (SELECT cid, SUM(1.0 / (60.0 + rnk)) AS rrf
             FROM per WHERE rn_in_page <= 3
             GROUP BY cid)
```

Formulas (k_RRF = 60):

```
chunk score   S(c) = Σ_legs  1 / (60 + rank_leg(c))     (only top-3 ranks
                                                        per leg per page)
page score    P(page) = Σ_{c ∈ page} S(c)
```

With the cap, a page contributes **at most 9 RRF terms** (3 legs × 3 ranks)
no matter how many chunks it has, so "a few strong matches" always beats
"many marginal ones".

## Per-page adjustments

```sql
final_score = (P + 0.005 * ln(1 + fetch_count)) * exp(-age_days / 14)
```

### Prominence (additive prior)

`+ 0.005 · ln(1 + fetch_count)` — a gentle bonus for pages the LLM actually
reads (every `fetch_page`/`fetch_pages` read bumps `fetch_count`). It is
**additive, not multiplicative**: with realistic RRF masses (0.05–0.15) it
nudges ties in favor of proven-useful pages without letting popularity
override topical evidence. Magnitude: `fetch_count` 1 → +0.0035, 10 →
+0.012, 100 → +0.023.

(A multiplicative `× (1 + ln(1+fc))` variant was tried and rejected — it
amplified RRF itself and let high-traffic pages dominate; additive is the
fix.)

### Freshness decay

`× exp(−age_days/14)` from `last_crawled` (clamped at 0 for never-crawled).
Half-life ≈ 9.7 days; a 30-day-old page keeps ~11.7% of its score. Combined
with the threshold, stale pages drop out of the serving set unless they are
extremely relevant — and the watchlist worker (`fetch_count` DESC,
`last_crawled` ASC) is what keeps frequently-used pages fresh.

### Filter

`pages.disabled = false` — a page can be soft-disabled (excluded from
results, kept in the DB) via `PATCH /api/pages/{url}`.

## Output

Top `k` rows, ordered by final score:

| Column | Notes |
|---|---|
| `url`, `title` | from `pages` |
| `snippet` | best-scoring chunk's text, whitespace-collapsed, truncated to 400 chars |
| `score` | final adjusted score (double precision) |
| `last_crawled` | timestamptz |
| `fetch_count` | prominence input, surfaced for transparency |

## Worked example (illustrative)

Query: *"postgresql documentation reference"*.
Page X: `fetch_count = 24`, `last_crawled` 3 days ago.

| Chunk | FTS rank | Trigram rank | Vector rank | S(c) |
|---|---|---|---|---|
| c1 | 1 | 2 | 1 | 1/61 + 1/62 + 1/61 = 0.04892 |
| c2 | 3 | — | 5 | 1/63 + 1/65 = 0.03126 |
| c3 | — | 4 | — | 1/64 = 0.01563 |
| **P(X)** | | | | **0.09580** |

```
prominence:  0.005 · ln(1 + 24) = 0.005 · ln 25 = 0.01609
freshness:   exp(−3/14)         = 0.8080
final:       (0.09580 + 0.01609) × 0.8080 = 0.0904
```

A weaker off-topic page (one chunk, rank ~8 in one leg, fresh):

```
S = 1/68 = 0.01471;  final ≈ 0.015  → far below threshold
```

The *relative* ordering — and the fact that X's mass comes from three
distinct legs at top ranks, not from fifty marginal ones — is what the top-3
cap enforces.

## Score scale and `SEARCH_MIN_SCORE`

Because scores are rank-based, the usable band is narrow and **shifts with
index size**: measured on a working index, the best off-topic page for a
deliberately off-topic query scored ≈ **0.10**, while the weakest relevant
page for on-topic queries scored ≈ **0.13+**. `SEARCH_MIN_SCORE` (default
**0.12**, `config.py`) sits in that gap:

- score ≥ 0.12 → serve local (zero provider cost)
- score < 0.12 → gateway, with the local rows still available as the
  degraded-mode fallback

The old default of 0.2 pre-dated the top-3 cap and effectively disabled the
local index (nearly every query hit a provider). After the cap, scores
compress into ~0.10–0.155, so 0.12 is the correct band. If you change the
index size dramatically or the ranking weights, re-measure:

```sql
SELECT url, score FROM fn_search_local('your on-topic query', NULL, 10);
SELECT url, score FROM fn_search_local('chocolate cake espresso recipe', NULL, 10);
```

and place the threshold between the two clusters.

## Changing the ranking

1. Edit `fn_search_local` in `src/wellisearch/schema.sql`.
2. Rebuild + recreate the container (the schema is re-applied at startup):
   `docker compose -f compose.yml build && docker compose -f compose.yml up -d`.
   — or apply immediately in a live DB:
   `docker compose -f compose.yml exec postgres psql -U wellington -d wellisearch -f ...`
   (no Python restart needed; the function lives in Postgres).
3. Re-run `tests/test_db.py` (it exercises `fn_search_local` end-to-end)
   and the e2e suite.
