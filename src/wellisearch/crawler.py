"""Crawl4AI REST client — the single crawling path (plan §3).

Verified live (2026-08) against crawl4ai 0.9.2:
  - auth header is `Authorization: Bearer <CRAWL4AI_API_KEY>`
    (401 without; `x-api-key` is rejected)
  - markdown endpoint: POST {CRAWL4AI_URL}/md  body {"url": "..."}
    → {"url", "filter", "query", "cache", "markdown", "success"}
"""
from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger("wellisearch.crawler")


class CrawlError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{url}: {message}")
        self.url = url
        self.message = message
        self.status = status

    def status_label(self) -> str:
        return f"http_{self.status}" if self.status else "error"


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = get_settings().CRAWL4AI_API_KEY
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


async def fit_markdown(url: str) -> str:
    """Crawl one URL → clean fit-markdown. Raises CrawlError on any failure."""
    s = get_settings()
    base = s.CRAWL4AI_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=s.CRAWL_TIMEOUT_S) as client:
        try:
            r = await client.post(f"{base}/md", json={"url": url}, headers=_headers())
        except httpx.HTTPError as e:
            raise CrawlError(url, f"network: {e}") from e

    if r.status_code in (401, 403):
        raise CrawlError(url, f"auth rejected ({r.status_code})", status=r.status_code)
    if r.status_code >= 400:
        raise CrawlError(url, f"crawl4ai http {r.status_code}: {r.text[:200]}", status=r.status_code)

    data = r.json()
    if not data.get("success"):
        raise CrawlError(url, f"crawl4ai failed: {str(data)[:200]}")
    md = data.get("markdown") or ""
    if not md.strip():
        raise CrawlError(url, "empty markdown returned")
    return md


async def health() -> tuple[bool, str]:
    """Reachability + auth check for /health."""
    s = get_settings()
    base = s.CRAWL4AI_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base}/health", headers=_headers())
            if r.status_code == 200:
                return True, "ok"
            return False, f"http {r.status_code}"
    except httpx.HTTPError as e:
        return False, str(e)
