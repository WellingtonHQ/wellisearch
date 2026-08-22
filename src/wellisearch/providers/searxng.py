"""SearXNG — optional keyless last-resort provider.

Verified live (2026-08): GET {SEARXNG_URL}/search?q=...&format=json; response
{query, results: [{url, title, content, ...}], ...}.
Kept only as a keyless backstop (metasearch scraping is unreliable from
server IPs — plan §1/§15). Drop from SEARCH_PROVIDERS if not running it.
"""
from __future__ import annotations

import httpx

from .base import Provider, ProviderError, Result


class SearxNG(Provider):
    name = "searxng"

    @property
    def configured(self) -> bool:
        return bool(self.s.SEARXNG_URL)

    async def search(self, query: str, num: int) -> list[Result]:
        url = f"{self.s.SEARXNG_URL.rstrip('/')}/search"
        params = {"q": query, "format": "json"}
        try:
            r = await self.client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}") from e

        if r.status_code >= 400:
            raise ProviderError(self.name, f"http {r.status_code}: {r.text[:200]}", status=r.status_code)

        data = r.json()
        out: list[Result] = []
        for item in data.get("results", []):
            u = (item.get("url") or "").strip()
            if not u:
                continue
            out.append(
                Result(
                    url=u,
                    title=self.clean_html(item.get("title") or u),
                    snippet=self.snippet(item.get("content") or ""),
                    score=item.get("score"),
                )
            )
        return out
