"""Configuration: all env knobs in one place (pydantic-settings).

Values come from the process environment (compose `env_file: .env` in the
container, or a loaded .env when running on the host).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env knobs in one place; values come from the process environment
    (compose `env_file: .env` in the container, or a loaded .env on the host)."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- postgres (host must be resolvable + reachable from the app container) ---
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "wellington"
    POSTGRES_PASSWORD: str = "change-me"
    POSTGRES_DB: str = "wellisearch"
    # Admin/maintenance DB used only to self-create the app DB at startup (§11).
    POSTGRES_ADMIN_DB: str = "postgres"
    DB_POOL_MIN_SIZE: int = 2
    DB_POOL_MAX_SIZE: int = 24
    # Fail checkout with PoolTimeout ("database busy") instead of hanging on the default 30 s.
    DB_POOL_TIMEOUT_S: float = 10.0

    # --- search providers (failover pool + default order; the dashboard can
    # override the order at runtime — see provider_state.sort_order) ---
    SEARCH_PROVIDERS: str = "tavily,brave,exa,youcom"
    TAVILY_API_KEY: str = ""
    TAVILY_QUOTA_MONTHLY: int = 1000
    BRAVE_API_KEY: str = ""
    BRAVE_QUOTA_MONTHLY: int = 1000
    EXA_API_KEY: str = ""
    EXA_QUOTA_MONTHLY: int = 1000
    YOUCOM_API_KEY: str = ""
    YOUCOM_QUOTA_MONTHLY: int = 1000
    PROVIDER_TIMEOUT_S: int = 20

    # --- embeddings (single source of truth; load-bearing) ---
    EMBED_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBED_DIMS: int = 384
    # ORT intra-op threads per embedding session (fastembed `threads=`).
    # Each concurrent embed spawns its own ORT thread pool; unbounded (all
    # cores) × 3-4 concurrent sessions oversubscribes the host and starves
    # Postgres. MiniLM is small — a couple of threads per session is plenty.
    EMBED_THREADS: int = 2
    # Reindex batch size (url-keyset pages): only one batch of fit_markdown is
    # resident at a time, so peak RSS stays flat regardless of index size
    # (loading the whole stale set up front OOM-killed long reindexes).
    REINDEX_BATCH_SIZE: int = 1000

    # --- search ---
    SEARCH_K: int = 5
    SEARCH_MAX_CRAWL: int = 5
    # Local-hit gate: fetch at least this many rows so the gate can see a
    # passing page that ranks well outside the top-k by score (score is
    # rank-only; semantically strong pages often sit far down it — e.g. a
    # vec-leg-only article behind dozens of lexically matching job postings).
    # Auto mode fetches SEARCH_GATE_MIN_K + SEARCH_TOP_BY_SIM rows so the union
    # candidate window has its full pool (docs/ranking.md). Cost: only the final
    # LIMIT and per-row gate columns grow (~+160 ms per 50 rows on the ~178k-chunk
    # index, well inside SEARCH_STATEMENT_TIMEOUT_MS).
    SEARCH_GATE_MIN_K: int = 100
    # Local-hit gate (condition 1): a passing row must cover at least this
    # fraction of the query's content words (`coverage` column, see
    # docs/ranking.md). Kept deliberately low: generic qualifier words ("best",
    # "top") that catalog pages omit must not veto them — similarity is the
    # primary topical filter.
    LOCAL_MIN_COVERAGE: float = 0.5
    # Local-hit gate (condition 2): a passing row must also have best-chunk
    # cosine similarity >= this (`similarity` column). Rejects pages that merely
    # contain the query's words scattered across a huge body — word lists, vocab
    # dumps — which coverage alone cannot tell apart. NULL similarity (no
    # embeddings / failed query embed) never passes; auto mode then defers to
    # the provider gateway.
    LOCAL_MIN_SIMILARITY: float = 0.3
    # Local-hit gate (condition 3): a passing row must cover at least this fraction
    # of the query's DISTINCTIVE words — those in <1% of the corpus, i.e. brand /
    # product names rather than common words (`distinctive_coverage` column). Default
    # 1.0: a page that misses every distinctive term is not about what was asked even
    # if it covers the common words ("baby bottles reviews" junk must not answer
    # "Playtex baby bottles reviews"). Queries with no rare words are unaffected (the
    # column is 1.0). See docs/ranking.md.
    LOCAL_MIN_DISTINCTIVE_COVERAGE: float = 1.0
    # A single strong page can satisfy auto mode even when fewer than SEARCH_K
    # pages pass. Keep this above the ordinary gate's floor so a marginal lone
    # hit still defers to the provider (see docs/ranking.md).
    LOCAL_PARTIAL_MIN_SIMILARITY: float = 0.55
    # Several ordinary gate-passing pages can also serve a partial answer,
    # even when no single chunk meets the stronger similarity threshold.
    LOCAL_PARTIAL_MIN_PASSING: int = 3
    # Auto-mode serving policies (docs/ranking.md): a union candidate window —
    # top-SEARCH_GATE_MIN_K by score plus the SEARCH_TOP_BY_SIM most similar
    # rows not already in it — so a semantically close page is always
    # considered even when lexical mass buries its score. Local mode serves
    # the raw index order untouched.
    SEARCH_TOP_BY_SIM: int = 20
    # Max results per registrable domain in auto-mode serving, so one site's
    # many near-duplicate pages (e.g. job-board postings) cannot flood the
    # answer set; local mode is uncapped.
    SEARCH_MAX_PER_DOMAIN: int = 2
    # Job-board de-rank for non-job-intent queries: rows whose URL matches a
    # SEARCH_JOB_BOARDS entry get score AND similarity multiplied by this
    # (coverage untouched, so the gate still sees them). A query matching any
    # SEARCH_JOB_INTENT_TERMS skips the penalty entirely.
    SEARCH_JOB_BOARD_PENALTY: float = 0.5
    SEARCH_JOB_BOARDS: str = "linkedin.com/jobs,indeed.com,glassdoor.com/Job,ziprecruiter.com/Jobs,monster.com,naukri.com,jobs.lever.co,boards.greenhouse.io"
    SEARCH_JOB_INTENT_TERMS: str = "job, jobs, hiring, open roles, careers"
    # Vector-leg row cap for fn_search_local (HNSW early-stop keeps it cheap);
    # a wider leg lets semantically close but lexically thin pages enter the
    # fusion at all instead of missing the pool entirely.
    SEARCH_VECTOR_LEG_LIMIT: int = 200
    # Legacy local-hit cutoff; now only for ranking (see docs/ranking.md).
    SEARCH_MIN_SCORE: float = 0.06
    STALE_HOURS: int = 72
    # Chunk token budget for chunk_markdown; must stay under MiniLM's hard
    # 512-token input cap (chunk.py estimates tokens as len(text)//4, so the
    # headroom absorbs over-estimates and keeps long chunks from truncating).
    MAX_CHUNK_TOKENS: int = 500
    # Per-statement backstop for the local search SQL (SET LOCAL, search only):
    # no query may hold a pooled connection for minutes. A timeout falls back
    # to the provider gateway (search_web.py) instead of stalling the request.
    SEARCH_STATEMENT_TIMEOUT_MS: int = 15000

    # --- fetch_pages truncation (swappable strategies) ---
    FETCH_DEFAULT_STRATEGY: str = "smart"  # even | head | priority | smart | tail
    FETCH_MAX_CHARS: int = 40000  # default total budget when max_chars omitted
    FETCH_PER_PAGE_CHARS: int = 12000  # default per-page cap
    FETCH_PROBE_TIMEOUT_S: float = 15.0  # per-tier timeout cap while a fetch crawls on demand
    FETCH_TIMEOUT_S: float = 45.0  # hard deadline for one on-demand crawl; past it the URL is re-queued
    # Grace after an on-demand probe's client leaves (cancel / deadline): its in-flight tier
    # attempt gets this long to finish and store, then we stop the orphan so it stops holding
    # its crawl slot + dedup entry. Default = one per-tier budget, so only *further* failover
    # attempts are cut off. Worker crawls have no grace — nothing left waiting on them.
    FETCH_ORPHAN_GRACE_S: float = 15.0

    # --- worker / queue (async indexing) ---
    WORKER_INTERVAL_MIN: float = 30
    WORKER_BUDGET_PER_RUN: int = 25
    REFRESH_MIN_AGE_HOURS: int = 72  # refresh pass skips pages whose last crawl is younger than this
    # Base delay for the watchlist-refresh failure backoff: after N consecutive
    # failed crawls a page waits base * 2^(N-1) hours (capped at one full refresh
    # cycle, see Settings.refresh_backoff_hours). Without it, dead pages are
    # retried on every worker tick (the 2026-09-13 refresh retry loop).
    REFRESH_BACKOFF_BASE_HOURS: float = 6.0
    WORKER_TICK_BUDGET_MIN: int = 15
    KICK_DEBOUNCE_S: int = 5
    QUEUE_MAX_ATTEMPTS: int = 3
    CRAWL_TIMEOUT_S: int = 45
    CRAWL_MAX_PARALLEL: int = 8
    LOG_RETENTION_DAYS: int = 90  # event_log / crawl_log / search_log prune age

    # --- native crawl engine (replaces the Crawl4AI path; design §6) ---
    CRAWL_MAX_REVIEWS: int = 5  # top reviews kept per product page (amazon, homedepot, ...)
    # CF (challenge) lane: a dedicated low-concurrency, high-timeout lane so a
    # Cloudflare/turnstile crawl never blocks the fast lane. The fast lane only
    # probes for a bot-wall and routes it here; the CF lane runs the full
    # challenge loop with its own pool + semaphore + timeout.
    CRAWL_CF_POOL_SIZE: int = 1
    CRAWL_CF_TIMEOUT_S: int = 300
    CRAWL_CHALLENGE_BUDGET_S: int = 40
    CRAWL_CHALLENGE_PARALLEL: int = 2
    CRAWL_HEADLESS: bool = False
    CRAWL_HTTP_TIER: bool = True
    # Launch backoff after a failed browser launch (see native-crawler-design.md §3.4).
    CRAWL_LAUNCH_RETRY_AFTER_S: float = 30.0
    CRAWL_MD_MAX_CHARS: int = 150000
    CRAWL_POOL_SIZE: int = 3
    CRAWL_PROFILE_DIR: str = "/profiles"
    CRAWL_PROFILE_MAX: int = 8
    CRAWL_REDDIT_COMMENT_RANKING: Literal["best", "score"] = "score"
    CRAWL_REDDIT_MAX_COMMENTS: int = 25  # highest-ranked comments kept per post
    CRAWL_SETTLE_S: float = 2.0
    # Hosts of known URL shorteners (e.g. Amazon's a.co): resolved to their final
    # URL before crawling, so policy/extractor selection sees the real site. Comma list.
    CRAWL_SHORT_URL_HOSTS: str = "a.co"
    CRAWL_STEALTH_TIER: bool = True
    CRAWL_STEALTH_TIMEOUT_S: int = 120
    # Crawl tiers only fetch read-only pages, so untrusted TLS certs are accepted by default.
    CRAWL_IGNORE_SSL_ERRORS: bool = True

    # --- server ---
    BIND_PORT: int = 8780
    WELLISEARCH_API_KEY: str = ""  # empty = open; set = require on REST + MCP

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    @property
    def provider_order(self) -> list[str]:
        """Provider failover order from SEARCH_PROVIDERS (comma list, lowercased)."""
        names = [p.strip().lower() for p in self.SEARCH_PROVIDERS.split(",")]
        return [n for n in names if n]

    @property
    def job_boards(self) -> tuple[str, ...]:
        """Job-board host[/path] prefixes from SEARCH_JOB_BOARDS (comma list)."""
        return tuple(e.strip() for e in self.SEARCH_JOB_BOARDS.split(",") if e.strip())

    @property
    def job_intent_terms(self) -> tuple[str, ...]:
        """Job-intent terms from SEARCH_JOB_INTENT_TERMS (comma list)."""
        return tuple(t.strip() for t in self.SEARCH_JOB_INTENT_TERMS.split(",") if t.strip())

    @property
    def short_url_hosts(self) -> frozenset[str]:
        """Shortener hosts from CRAWL_SHORT_URL_HOSTS (comma list, lowercased)."""
        return frozenset(h.strip().lower() for h in self.CRAWL_SHORT_URL_HOSTS.split(",") if h.strip())

    def env_quota_limit(self, provider: str) -> int | None:
        """Default monthly quota for a provider (env-backed). None = unknown."""
        raw = {
            "tavily": self.TAVILY_QUOTA_MONTHLY,
            "brave": self.BRAVE_QUOTA_MONTHLY,
            "exa": self.EXA_QUOTA_MONTHLY,
            "youcom": self.YOUCOM_QUOTA_MONTHLY,
        }.get(provider)
        if raw is None or raw <= 0:
            return None
        return int(raw)

    def refresh_backoff_hours(self, streak: int) -> float:
        """Refresh delay in hours after `streak` consecutive failed crawls.

        Doubles from REFRESH_BACKOFF_BASE_HOURS (6h → 12h → 24h → ...) up to one
        full refresh cycle (REFRESH_MIN_AGE_HOURS), so a dead page is retried at
        most once per cycle instead of on every worker tick.
        """
        if streak <= 0:
            return 0.0
        delay = self.REFRESH_BACKOFF_BASE_HOURS * (2 ** (streak - 1))
        return min(delay, float(self.REFRESH_MIN_AGE_HOURS))

    def conninfo(self, dbname: str | None = None) -> str:
        """psycopg connection string for ``dbname`` (default: POSTGRES_DB)."""
        return (
            f"host={self.POSTGRES_HOST} port={self.POSTGRES_PORT} "
            f"user={self.POSTGRES_USER} password={self.POSTGRES_PASSWORD} "
            f"dbname={dbname or self.POSTGRES_DB} "
            "sslmode=disable connect_timeout=5"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached Settings instance (one per process)."""
    return Settings()
