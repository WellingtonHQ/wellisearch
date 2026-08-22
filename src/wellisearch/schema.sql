-- wellisearch schema (Postgres 18 + pgvector + pg_trgm)
-- Applied idempotently at app startup (db.py). Safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- pages: one row per indexed URL
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY,
  title TEXT,
  domain TEXT,
  fit_markdown TEXT,
  content_hash TEXT, -- sha256 of fit_markdown; unchanged → skip re-embed
  embedding_model TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_crawled TIMESTAMPTZ,
  last_status TEXT, -- ok | unchanged | error | http_<code>
  crawl_count INT NOT NULL DEFAULT 0,
  fetch_count INT NOT NULL DEFAULT 0, -- the priority/prominence counter
  search_hit_count INT NOT NULL DEFAULT 0,
  disabled BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS pages_domain_idx ON pages (domain);

-- ---------------------------------------------------------------------------
-- chunks: per-chunk content + tsv (FTS) + embedding (vector)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
  id BIGSERIAL PRIMARY KEY,
  url TEXT NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
  seq INT NOT NULL,
  text TEXT NOT NULL,
  tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding VECTOR(384),
  last_crawled TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_url_seq_uq ON chunks (url, seq);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_trgm_gin ON chunks USING gin (text gin_trgm_ops);
-- HNSW: built lazily/concurrently-safe here; at our scale a blocking build is fine.
CREATE INDEX IF NOT EXISTS chunks_vec_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- crawl_queue: background crawl work (durable across restarts)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_queue (
  id BIGSERIAL PRIMARY KEY,
  url TEXT NOT NULL,
  source TEXT NOT NULL, -- 'search' | 'manual'
  enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempts INT NOT NULL DEFAULT 0,
  last_error TEXT,
  status TEXT NOT NULL DEFAULT 'pending' -- pending | in_flight | done | failed
);
CREATE UNIQUE INDEX IF NOT EXISTS crawl_queue_url_pending_uq
  ON crawl_queue(url) WHERE status IN ('pending','in_flight');

-- ---------------------------------------------------------------------------
-- search_log: every search (query, source/provider, urls, titles, snippets)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  query TEXT NOT NULL,
  source TEXT NOT NULL, -- 'local' | 'tavily' | 'brave' | 'searxng'
  local_hits INT,
  results JSONB
);
CREATE INDEX IF NOT EXISTS search_log_ts_idx ON search_log (ts DESC);

-- ---------------------------------------------------------------------------
-- provider_quota: monthly usage ledger (gateway failover + dashboard)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_quota (
  provider TEXT NOT NULL, -- 'tavily' | 'brave'
  month TEXT NOT NULL, -- 'YYYY-MM'
  used INT NOT NULL DEFAULT 0,
  quota_limit INT, -- NULL = unknown; gateway still fails over on 429
  PRIMARY KEY (provider, month)
);

-- ---------------------------------------------------------------------------
-- crawl_log: every crawl attempt (trigger, status, timing, chunks written)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  url TEXT NOT NULL,
  trigger TEXT NOT NULL, -- 'search' | 'fetch' | 'refresh' | 'manual'
  status TEXT NOT NULL, -- 'ok' | 'unchanged' | 'error' | 'http_<code>'
  ms INT,
  chunks_written INT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS crawl_log_ts_idx ON crawl_log (ts DESC);

-- ---------------------------------------------------------------------------
-- provider_state: runtime provider gateway state (toggles persist across
-- restarts; env supplies the defaults — see PATCH /api/providers/{name})
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_state (
  provider TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT true,
  limit_override INT, -- runtime override of the env monthly limit
  last_served TIMESTAMPTZ,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- fn_search_local(query, qvec, k) — the hybrid ranking core.
--
-- Three legs, each top-50 chunks:
--   1. FTS:      ts_rank_cd against plainto_tsquery (AND of all lexemes).
--                If no chunk contains every lexeme, fall back to the same
--                lexemes OR-ed (multi-word queries rarely put all their
--                words into a single chunk).
--   2. Trigram:  similarity / word_similarity against chunk text + page title
--                (covers typos/fragments the LLM emits)
--   3. Vector:   embedding <=> qvec (skipped when qvec IS NULL)
--
-- RRF fusion (k=60); per page, only the 3 best ranks of each leg count (so
-- many marginal matches cannot outrank a few strong ones). Then per-page
-- adjustments:
--   + 0.005 * ln(1 + p.fetch_count) — prominence: a gentle prior for pages
--                                   the LLM actually reads; additive so it
--                                   nudges ties instead of overriding
--                                   topical evidence
--   × exp(-age_days/14)            — freshness decay from last_crawled
-- Filter: p.disabled = false.
--
-- Returns: url, title, snippet (top-scoring chunk text, ~400 chars), score,
--          last_crawled, fetch_count.
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_search_local(query TEXT, qvec VECTOR(384), k INT)
RETURNS TABLE (
  url TEXT,
  title TEXT,
  snippet TEXT,
  score DOUBLE PRECISION,
  last_crawled TIMESTAMPTZ,
  fetch_count INT
)
LANGUAGE sql
STABLE
AS $$
  WITH q AS (
    SELECT
      NULLIF(plainto_tsquery('english', query), to_tsquery('english', '')) AS tsq_and,
      CASE
        WHEN plainto_tsquery('english', query) = to_tsquery('english', '') THEN NULL
        ELSE to_tsquery('english', replace(
               plainto_tsquery('english', query)::text, ' & ', ' | '))
      END AS tsq_or
  ),
  fts_and AS (
    SELECT c.id AS cid, ts_rank_cd(c.tsv, q.tsq_and) AS rk
    FROM chunks c, q
    WHERE q.tsq_and IS NOT NULL AND c.tsv @@ q.tsq_and
  ),
  fts AS (
    SELECT cid, rk FROM fts_and
    UNION ALL
    SELECT c.id, ts_rank_cd(c.tsv, q.tsq_or)
    FROM chunks c, q
    WHERE q.tsq_or IS NOT NULL AND c.tsv @@ q.tsq_or
      AND NOT EXISTS (SELECT 1 FROM fts_and)
  ),
  ftsr AS (
    SELECT cid, ROW_NUMBER() OVER (ORDER BY rk DESC) AS rnk FROM fts
  ),
  trg AS (
    SELECT c.id AS cid,
           ROW_NUMBER() OVER (ORDER BY
             GREATEST(similarity(c.text, query),
                      word_similarity(c.text, query),
                      similarity(p.title, query)) DESC) AS rnk
    FROM chunks c
    JOIN pages p ON p.url = c.url
    WHERE GREATEST(similarity(c.text, query),
                   word_similarity(c.text, query),
                   similarity(p.title, query)) > 0.05
  ),
  vec AS (
    SELECT c.id AS cid,
           ROW_NUMBER() OVER (ORDER BY c.embedding <=> qvec) AS rnk
    FROM chunks c
    WHERE c.embedding IS NOT NULL AND qvec IS NOT NULL
  ),
  leg AS (
    SELECT 'fts' AS leg, cid, rnk FROM ftsr WHERE rnk <= 50
    UNION ALL
    SELECT 'trg' AS leg, cid, rnk FROM trg WHERE rnk <= 50
    UNION ALL
    SELECT 'vec' AS leg, cid, rnk FROM vec WHERE rnk <= 50
  ),
  per AS (
    SELECT leg, cid, rnk,
           ROW_NUMBER() OVER (PARTITION BY leg, c.url ORDER BY rnk) AS rn_in_page
    FROM leg
    JOIN chunks c ON c.id = leg.cid
  ),
  perpage AS (
    SELECT cid, SUM(1.0 / (60.0 + rnk)) AS rrf
    FROM per
    WHERE rn_in_page <= 3
    GROUP BY cid
  ),
  pagescore AS (
    SELECT c.url AS url, SUM(perpage.rrf) AS score
    FROM perpage
    JOIN chunks c ON c.id = perpage.cid
    GROUP BY c.url
  ),
  bestchunk AS (
    SELECT DISTINCT ON (c.url) c.url AS url, c.text AS text
    FROM perpage
    JOIN chunks c ON c.id = perpage.cid
    ORDER BY c.url, perpage.rrf DESC
  )
  SELECT
    p.url AS url,
    p.title AS title,
    left(regexp_replace(bc.text, '\s+', ' ', 'g'), 400) AS snippet,
    (ps.score
     + 0.005 * ln(1.0 + p.fetch_count))
     * exp(-GREATEST(EXTRACT(EPOCH FROM (now() - p.last_crawled)) / 1209600.0, 0))
     AS score,
    p.last_crawled AS last_crawled,
    p.fetch_count AS fetch_count
  FROM pagescore ps
  JOIN pages p ON p.url = ps.url AND p.disabled = false
  JOIN bestchunk bc ON bc.url = ps.url
  ORDER BY
    (ps.score
     + 0.005 * ln(1.0 + p.fetch_count))
     * exp(-GREATEST(EXTRACT(EPOCH FROM (now() - p.last_crawled)) / 1209600.0, 0))
    DESC
  LIMIT k;
$$;
