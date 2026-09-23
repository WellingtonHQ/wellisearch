"""Configuration: all env knobs in one place (pydantic-settings).

Values come from the process environment (compose `env_file: .env` in the
container, or a loaded .env when running on the host).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env knobs in one place; values come from the process environment
    (compose `env_file: .env` in the container, or a loaded .env on the host)."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------------------
    # Postgres
    # ---------------------------------------------------------------------------

    # Host must be resolvable + reachable from the app container.
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "wellington"
    POSTGRES_PASSWORD: str = "change-me"
    POSTGRES_DB: str = "wellisearch"
    # Admin/maintenance DB used only to self-create the app DB at startup (§11).
    POSTGRES_ADMIN_DB: str = "postgres"
    # Connection pool sizing (db.py AsyncConnectionPool).
    DB_POOL_MIN_SIZE: int = 2
    DB_POOL_MAX_SIZE: int = 12

    # ---------------------------------------------------------------------------
    # Search Providers
    # ---------------------------------------------------------------------------

    # Failover pool + default order; the dashboard can override the order at
    # runtime (see provider_state.sort_order).
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

    # ---------------------------------------------------------------------------
    # Embeddings
    # ---------------------------------------------------------------------------

    # Single source of truth; load-bearing.
    EMBED_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBED_DIMS: int = 384
    # ORT intra-op threads per embedding session (fastembed `threads=`).
    # Each concurrent embed spawns its own ORT thread pool; unbounded (all
    # cores) × 3-4 concurrent sessions oversubscribes the host and starves
    # Postgres. MiniLM is small — a couple of threads per session is plenty.
    EMBED_THREADS: int = 2

    # ---------------------------------------------------------------------------
    # Search
    # ---------------------------------------------------------------------------

    SEARCH_K: int = 5
    # Max chars of a snippet in search results (local + provider).
    SNIPPET_MAX_LEN: int = 400
    SEARCH_MAX_CRAWL: int = 5
    # Local-hit gate: fetch at least this many rows so the coverage gate can
    # see a full-coverage page that ranks just outside the top-k by score.
    SEARCH_GATE_MIN_K: int = 10
    # Local-hit gate: serve local if any top result's `coverage`
    # (fn_search_local column, see docs/ranking.md) is >= this.
    LOCAL_MIN_COVERAGE: float = 0.75
    # Legacy local-hit cutoff; now only for ranking (see docs/ranking.md).
    SEARCH_MIN_SCORE: float = 0.06
    STALE_HOURS: int = 72
    MAX_CHUNK_TOKENS: int = 800
    # Per-statement backstop for the local search SQL (SET LOCAL, search only):
    # no query may hold a pooled connection for minutes. A timeout falls back
    # to the provider gateway (search_web.py) instead of stalling the request.
    SEARCH_STATEMENT_TIMEOUT_MS: int = 15000

    # ---------------------------------------------------------------------------
    # Fetch Pages Truncation
    # ---------------------------------------------------------------------------

    # Swappable strategies.
    FETCH_DEFAULT_STRATEGY: str = "smart"  # even | head | priority | smart | tail
    FETCH_MAX_CHARS: int = 40000  # default total budget when max_chars omitted
    FETCH_PER_PAGE_CHARS: int = 12000  # default per-page cap

    # ---------------------------------------------------------------------------
    # Worker / Queue
    # ---------------------------------------------------------------------------

    # Async indexing.
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
    LOG_RETENTION_DAYS: int = 30  # event_log / crawl_log / search_log prune age

    # ---------------------------------------------------------------------------
    # Native Crawl Engine
    # ---------------------------------------------------------------------------

    # Replaces the Crawl4AI path; design §6.
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
    CRAWL_SETTLE_S: float = 2.0
    CRAWL_STEALTH_TIER: bool = True
    CRAWL_STEALTH_TIMEOUT_S: int = 120
    # The crawl tiers only fetch public read-only pages (they never send data),
    # so untrusted TLS certs (self-signed, expired, name-mismatch) are accepted
    # by default and the content is indexed as-is; set to False for strict
    # certificate verification. Applies to all three transport tiers.
    CRAWL_IGNORE_SSL_ERRORS: bool = True

    # ---------------------------------------------------------------------------
    # Server
    # ---------------------------------------------------------------------------

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
