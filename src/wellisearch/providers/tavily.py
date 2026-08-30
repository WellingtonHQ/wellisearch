"""Tavily Search API — agent-optimized structured results.

Verified live (2026-08): POST https://api.tavily.com/search with
Authorization: Bearer <key>; body {"query", "max_results"}; response
{query, answer, images, results: [{url, title, content, score, ...}], ...}.
"""
from __future__ import annotations

import httpx

from .base import Provider, ProviderError, Result


class Tavily(Provider):
    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    @property
    def configured(self) -> bool:
        return bool(self.s.TAVILY_API_KEY)

    async def search(
        self,
        query: str,
        num: int,
    ) -> list[Result]:
        headers = {"Authorization": f"Bearer {self.s.TAVILY_API_KEY}"}
        body = {"query": query, "max_results": max(1, num)}
        try:
            r = await self.client.post(self.ENDPOINT, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}") from e

        if r.status_code in (401, 403):
            raise ProviderError(self.name, f"auth rejected ({r.status_code})", status=r.status_code)
        if r.status_code in (402, 429):
            raise ProviderError(self.name, f"quota exhausted ({r.status_code})", status=r.status_code)
        if r.status_code >= 400:
            raise ProviderError(self.name, f"http {r.status_code}: {r.text[:200]}", status=r.status_code)

        data = r.json()
        out: list[Result] = []
        for item in data.get("results", []):
            url = (item.get("url") or "").strip()
            if not url:
                continue
            out.append(
                Result(
                    url=url,
                    title=self.clean_html(item.get("title") or url),
                    snippet=self.snippet(item.get("content") or ""),
                    score=item.get("score"),
                )
            )
        return out
