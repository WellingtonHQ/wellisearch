-- wellisearch schema (Postgres 18 + pgvector + pg_trgm)
-- Applied idempotently at app startup (db.py). Safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- pages: one row per indexed URL
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
  url            text PRIMARY KEY,
  title          text,
  domain         text,
  fit_markdown   text,
  content_hash   text,               -- sha256 of fit_markdown; unchanged → skip re-embed
  embedding_model text,
  first_seen     timestamptz NOT NULL DEFAULT now(),
  last_crawled   timestamptz,
  last_status    text,               -- ok | unchanged | error | http_<code>
  crawl_count    int NOT NULL DEFAULT 0,
  fetch_count    int NOT NULL DEFAULT 0,        -- the priority/prominence counter
  search_hit_count int NOT NULL DEFAULT 0,
  disabled       boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS pages_domain_idx ON pages (domain);

-- ---------------------------------------------------------------------------
-- chunks: per-chunk content + tsv (FTS) + embedding (vector)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
  id           bigserial PRIMARY KEY,
  url          text NOT NULL REFERENCES pages(url) ON DELETE CASCADE,
  seq          int  NOT NULL,
  text         text NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding    vector(384),
  last_crawled timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_url_seq_uq ON chunks (url, seq);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin   ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_trgm_gin  ON chunks USING gin (text gin_trgm_ops);
-- HNSW: built lazily/concurrently-safe here; at our scale a blocking build is fine.
CREATE INDEX IF NOT EXISTS chunks_vec_hnsw  ON chunks USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- crawl_queue: background crawl work (durable across restarts)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_queue (
  id bigserial PRIMARY KEY,
  url text NOT NULL,
  source text NOT NULL,              -- 'search' | 'manual'
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  last_error text,
  status text NOT NULL DEFAULT 'pending'   -- pending | in_flight | done | failed
);
CREATE UNIQUE INDEX IF NOT EXISTS crawl_queue_url_pending_uq
  ON crawl_queue(url) WHERE status IN ('pending','in_flight');

-- ---------------------------------------------------------------------------
-- search_log: every search (query, source/provider, urls, titles, snippets)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  query text NOT NULL,
  source text NOT NULL,              -- 'local' | 'tavily' | 'brave' | 'searxng'
  local_hits int,
  results jsonb
);
CREATE INDEX IF NOT EXISTS search_log_ts_idx ON search_log (ts DESC);

-- ---------------------------------------------------------------------------
-- provider_quota: monthly usage ledger (gateway failover + dashboard)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_quota (
  provider text NOT NULL,            -- 'tavily' | 'brave'
  month    text NOT NULL,            -- 'YYYY-MM'
  used     int NOT NULL DEFAULT 0,
  quota_limit int,                   -- NULL = unknown; gateway still fails over on 429
  PRIMARY KEY (provider, month)
);

-- ---------------------------------------------------------------------------
-- crawl_log: every crawl attempt (trigger, status, timing, chunks written)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  url text NOT NULL,
  trigger text NOT NULL,             -- 'search' | 'fetch' | 'refresh' | 'manual'
  status text NOT NULL,              -- 'ok' | 'unchanged' | 'error' | 'http_<code>'
  ms int,
  chunks_written int,
  detail text
);
CREATE INDEX IF NOT EXISTS crawl_log_ts_idx ON crawl_log (ts DESC);

-- ---------------------------------------------------------------------------
-- provider_state: runtime provider gateway state (toggles persist across
-- restarts; env supplies the defaults — see PATCH /api/providers/{name})
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_state (
  provider    text PRIMARY KEY,
  enabled     boolean NOT NULL DEFAULT true,
  limit_override int,                -- runtime override of the env monthly limit
  last_served timestamptz,
  last_error  text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- fn_search_local(query, qvec, k) — the hybrid ranking core.
--
-- Three legs, each top-50 chunks:
--   1. FTS:      ts_rank_cd against plainto_tsquery (falls back to
--                websearch_to_tsquery when plainto yields an empty query)
--   2. Trigram:  similarity / word_similarity against chunk text + page title
--                (covers typos/fragments the LLM emits)
--   3. Vector:   embedding <=> qvec (skipped when qvec IS NULL)
--
-- RRF fusion (k=60) of the three ranked lists per chunk id, summed per page,
-- then per-page boosts:
--   × (1 + ln(1 + p.fetch_count))    — prominence (pages the LLM actually reads)
--   × exp(-age_days/14)             — freshness decay from last_crawled
-- Filter: p.disabled = false.
--
-- Returns: url, title, snippet (top-scoring chunk text, ~400 chars), score,
--          last_crawled, fetch_count.
-- ===========================================================================
CREATE OR REPLACE FUNCTION fn_search_local(query text, qvec vector(384), k int)
RETURNS TABLE (
  url text,
  title text,
  snippet text,
  score double precision,
  last_crawled timestamptz,
  fetch_count int
)
LANGUAGE sql
STABLE
AS $$
  WITH q AS (
    SELECT COALESCE(
      NULLIF(plainto_tsquery('english', query), to_tsquery('english', '')),
      websearch_to_tsquery('english', query)
    ) AS tsq
  ),
  fts AS (
    SELECT c.id AS cid,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC) AS rnk
    FROM chunks c, q
    WHERE c.tsv @@ q.tsq
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
    SELECT cid, rnk FROM fts WHERE rnk <= 50
    UNION ALL
    SELECT cid, rnk FROM trg WHERE rnk <= 50
    UNION ALL
    SELECT cid, rnk FROM vec WHERE rnk <= 50
  ),
  per AS (
    SELECT cid, SUM(1.0 / (60.0 + rnk)) AS rrf
    FROM leg
    GROUP BY cid
  ),
  pagescore AS (
    SELECT c.url AS url, SUM(per.rrf) AS score
    FROM per
    JOIN chunks c ON c.id = per.cid
    GROUP BY c.url
  ),
  bestchunk AS (
    SELECT DISTINCT ON (c.url) c.url AS url, c.text AS text
    FROM per
    JOIN chunks c ON c.id = per.cid
    ORDER BY c.url, per.rrf DESC
  )
  SELECT
    p.url AS url,
    p.title AS title,
    left(regexp_replace(bc.text, '\s+', ' ', 'g'), 400) AS snippet,
    (ps.score
     * (1.0 + ln(1.0 + p.fetch_count))
     * exp(-GREATEST(EXTRACT(EPOCH FROM (now() - p.last_crawled)) / 1209600.0, 0))
    ) AS score,
    p.last_crawled AS last_crawled,
    p.fetch_count AS fetch_count
  FROM pagescore ps
  JOIN pages p ON p.url = ps.url AND p.disabled = false
  JOIN bestchunk bc ON bc.url = ps.url
  ORDER BY
    (ps.score
     * (1.0 + ln(1.0 + p.fetch_count))
     * exp(-GREATEST(EXTRACT(EPOCH FROM (now() - p.last_crawled)) / 1209600.0, 0))
    ) DESC
  LIMIT k;
$$;
