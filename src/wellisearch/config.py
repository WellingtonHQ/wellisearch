"""Configuration: all env knobs in one place (pydantic-settings).

Values come from the process environment (compose `env_file: .env` in the
container, or a loaded .env when running on the host).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- postgres (shared infra container; "postgres" is the network alias) ---
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "wellington"
    POSTGRES_PASSWORD: str = "change-me"
    POSTGRES_DB: str = "wellisearch"
    # Admin/maintenance DB used only to self-create the app DB at startup (§11).
    POSTGRES_ADMIN_DB: str = "shared"

    # --- crawl4ai (the single crawling path) ---
    CRAWL4AI_URL: str = "http://crawl4ai:11235"
    CRAWL4AI_API_KEY: str = ""

    # --- search providers (ordered priority; first success serves) ---
    SEARCH_PROVIDERS: str = "tavily,brave,searxng"
    TAVILY_API_KEY: str = ""
    TAVILY_QUOTA_MONTHLY: int = 1000
    BRAVE_API_KEY: str = ""
    BRAVE_QUOTA_MONTHLY: int = 1000
    SEARXNG_URL: str = "http://searxng:8080"
    PROVIDER_TIMEOUT_S: int = 20

    # --- embeddings (single source of truth; load-bearing) ---
    EMBED_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBED_DIMS: int = 384

    # --- search ---
    SEARCH_K: int = 5
    SEARCH_MAX_CRAWL: int = 5
    SEARCH_MIN_SCORE: float = 0.12
    STALE_HOURS: int = 72
    MAX_CHUNK_TOKENS: int = 800

    # --- fetch_pages truncation (swappable strategies) ---
    FETCH_DEFAULT_STRATEGY: str = "smart"  # smart | head | tail | even | priority
    FETCH_MAX_CHARS: int = 40000  # default total budget when max_chars omitted
    FETCH_PER_PAGE_CHARS: int = 12000  # default per-page cap

    # --- worker / queue (async indexing) ---
    WORKER_INTERVAL_MIN: int = 30
    WORKER_BUDGET_PER_RUN: int = 25
    WORKER_TICK_BUDGET_MIN: int = 15
    KICK_DEBOUNCE_S: int = 5
    QUEUE_MAX_ATTEMPTS: int = 3
    CRAWL_TIMEOUT_S: int = 45
    CRAWL_MAX_PARALLEL: int = 3

    # --- server ---
    BIND_PORT: int = 8780
    WELLISEARCH_API_KEY: str = ""  # empty = open; set = require on REST + MCP

    # ------------------------------------------------------------------ helpers

    @property
    def provider_order(self) -> list[str]:
        names = [p.strip().lower() for p in self.SEARCH_PROVIDERS.split(",")]
        return [n for n in names if n]

    def env_quota_limit(self, provider: str) -> int | None:
        """Default monthly quota for a provider (env-backed). None = unknown."""
        raw = {
            "tavily": self.TAVILY_QUOTA_MONTHLY,
            "brave": self.BRAVE_QUOTA_MONTHLY,
            "searxng": 0,
        }.get(provider)
        if raw is None or raw <= 0:
            return None
        return int(raw)

    def conninfo(self, dbname: str | None = None) -> str:
        return (
            f"host={self.POSTGRES_HOST} port={self.POSTGRES_PORT} "
            f"user={self.POSTGRES_USER} password={self.POSTGRES_PASSWORD} "
            f"dbname={dbname or self.POSTGRES_DB} "
            "sslmode=disable connect_timeout=5"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
