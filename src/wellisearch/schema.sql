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
  source TEXT NOT NULL, -- 'local' | 'tavily' | 'brave' | 'exa' | 'youcom'
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
  trigger TEXT NOT NULL, -- 'search' | 'fetch' | 'refresh' | 'manual' | 'recrawl'
  status TEXT NOT NULL, -- 'ok' | 'unchanged' | 'error' | 'http_<code>'
  ms INT,
  chunks_written INT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS crawl_log_ts_idx ON crawl_log (ts DESC);

-- ---------------------------------------------------------------------------
-- event_log: operational events (worker ticks, provider gateway, admin
-- actions, lifecycle) — the "message + info" stream behind the dashboard
-- log view. Crawls/searches stay in their own tables; this carries the rest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  message TEXT NOT NULL,
  info JSONB
);
CREATE INDEX IF NOT EXISTS event_log_ts_idx ON event_log (ts DESC);

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
-- fn_search_local(query, qvec, k) — the hybrid ranking core: FTS + trigram +
-- vector legs (each top-50), RRF fusion with a per-page top-3 cap, then
-- prominence/freshness adjustments; disabled pages filtered out.
-- Design, formulas, and calibration: docs/ranking.md (trigram leg:
-- docs/trigram-rewrite.md).
-- ===========================================================================
-- DROP first: CREATE OR REPLACE cannot change the return type (adding a
-- column), and nothing references this function besides the app at runtime.
DROP FUNCTION IF EXISTS fn_search_local(TEXT, VECTOR(384), INT);
CREATE FUNCTION fn_search_local(query TEXT, qvec VECTOR(384), k INT)
RETURNS TABLE (
  url TEXT,
  title TEXT,
  snippet TEXT,
  score DOUBLE PRECISION,
  coverage DOUBLE PRECISION,
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
      END AS tsq_or,
      (SELECT count(*) FROM chunks) AS nchunks
  ),
  -- Distinctive query words: those in < 1% of the corpus (per-word GIN index
  -- counts, a few ms each). Their OR set stays small even inside long
  -- queries, so it is cheap to scan — unlike the common-word OR set (tens of
  -- thousands of rows, 2.4-6 s on this corpus). The pool serves pages that
  -- contain the distinctive word but not every query word.
  words AS (
    SELECT DISTINCT unnest(regexp_split_to_array(q.tsq_and::text, ' & ')) AS lexeme
    FROM q
    WHERE q.tsq_and IS NOT NULL
  ),
  rare AS (
    SELECT string_agg(lexeme, ' | ') AS words
    FROM words
    WHERE (SELECT count(*) FROM chunks c
           WHERE c.tsv @@ to_tsquery('english', words.lexeme)) * 100
          < (SELECT nchunks FROM q)
  ),
  -- url rides along in every leg so the fusion never joins back to chunks.
  -- FTS candidate set: the AND set (every lexeme) when non-empty, else the
  -- OR set — both index-backed, never a full-corpus scan (docs/ranking.md).
  fts_and AS (
    SELECT c.id AS cid, c.url AS url, left(c.text, 1000) AS text,
           ts_rank_cd(c.tsv, q.tsq_and) AS rk
    FROM chunks c, q
    WHERE q.tsq_and IS NOT NULL AND c.tsv @@ q.tsq_and
  ),
  fts AS (
    SELECT cid, url, text, rk FROM fts_and
    UNION ALL
    SELECT c.id, c.url, left(c.text, 1000), ts_rank_cd(c.tsv, q.tsq_or)
    FROM chunks c, q
    WHERE q.tsq_or IS NOT NULL AND c.tsv @@ q.tsq_or
      AND NOT EXISTS (SELECT 1 FROM fts_and)
  ),
  ftsr AS (
    SELECT cid, url, text, ROW_NUMBER() OVER (ORDER BY rk DESC) AS rnk
    FROM fts
  ),
  -- Bounded trigram leg: re-ranks a small index-backed candidate pool
  -- (FTS-AND top 50 + distinctive-word OR top 50) with similarity()/
  -- word_similarity(); design history in docs/trigram-rewrite.md.
  trg AS (
    SELECT cand.cid, cand.url,
           ROW_NUMBER() OVER (ORDER BY
               -- Prefix only: some chunks run to ~1 MB of text and
               -- similarity() cost scales with it (637 ms over 190 full
               -- chunks vs 11 ms over the 1000-char head); the head of a
               -- chunk is the most representative part anyway.
               GREATEST(similarity(cand.text, query),
                        word_similarity(cand.text, query)) DESC) AS rnk
    FROM (
      SELECT DISTINCT ON (cid) cid, url, text
      FROM (
        SELECT cid, url, text FROM ftsr WHERE rnk <= 50
        UNION ALL
        (SELECT c.id, c.url, left(c.text, 1000)
         FROM chunks c
         WHERE (SELECT words FROM rare) IS NOT NULL
           AND c.tsv @@ to_tsquery('english', (SELECT words FROM rare))
         ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', (SELECT words FROM rare))) DESC
         LIMIT 50)
      ) u
      ORDER BY cid
    ) cand
  ),
  -- Vector leg: inner top-50 via ORDER BY + LIMIT is the canonical pgvector
  -- shape — the planner streams from the HNSW index (chunks_vec_hnsw) and
  -- stops after 50 rows. Without the LIMIT the planner cannot early-stop and
  -- falls back to a full seq scan + sort over every embedding (~10-30 s at
  -- 1.4M chunks), which dominated search latency.
  vec AS (
    SELECT t.cid, t.url, ROW_NUMBER() OVER (ORDER BY t.d) AS rnk
    FROM (
      SELECT c.id AS cid, c.url AS url, c.embedding <=> qvec AS d
      FROM chunks c
      WHERE c.embedding IS NOT NULL AND qvec IS NOT NULL
      ORDER BY c.embedding <=> qvec
      LIMIT 50
    ) t
  ),
  leg AS (
    SELECT 'fts' AS leg, cid, url, rnk FROM ftsr WHERE rnk <= 50
    UNION ALL
    SELECT 'trg' AS leg, cid, url, rnk FROM trg WHERE rnk <= 50
    UNION ALL
    SELECT 'vec' AS leg, cid, url, rnk FROM vec WHERE rnk <= 50
  ),
  per AS (
    SELECT leg, cid, url, rnk,
           ROW_NUMBER() OVER (PARTITION BY leg, url ORDER BY rnk) AS rn_in_page
    FROM leg
  ),
  perpage AS (
    SELECT cid, url, SUM(1.0 / (60.0 + rnk)) AS rrf
    FROM per
    WHERE rn_in_page <= 3
    GROUP BY cid, url
  ),
  pagescore AS (
    SELECT url, SUM(rrf) AS score
    FROM perpage
    GROUP BY url
  ),
  -- Snippet: only the ~k winning pages are joined back to chunks (nested
  -- loop on the PK) to fetch their best chunk text.
  best AS (
    SELECT DISTINCT ON (url) cid, url
    FROM perpage
    ORDER BY url, rrf DESC
  ),
  bestchunk AS (
    SELECT b.url AS url, left(regexp_replace(c.text, '\s+', ' ', 'g'), 400) AS snippet
    FROM best b
    JOIN chunks c ON c.id = b.cid
  )
  SELECT
    p.url AS url,
    p.title AS title,
    bc.snippet AS snippet,
    (ps.score
     + 0.005 * ln(1.0 + p.fetch_count))
     * exp(-GREATEST(EXTRACT(EPOCH FROM (now() - p.last_crawled)) / 1209600.0, 0))
     AS score,
    -- Coverage: fraction of the query's content words in title+body (docs/ranking.md).
    -- left(...): to_tsvector overfits a 1 MB row type — pages with multi-MB
    -- fit_markdown (raw JSON blobs) overflowed it and crashed the function.
    (SELECT count(*) FROM words
       WHERE to_tsvector('english', COALESCE(p.title, '') || ' ' || left(COALESCE(p.fit_markdown, ''), 100000))
             @@ to_tsquery('english', words.lexeme)) * 1.0
    / NULLIF((SELECT count(*) FROM words), 0) AS coverage,
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
