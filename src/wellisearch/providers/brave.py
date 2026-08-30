"""Brave Search API — independent 30B+ index.

Verified live (2026-08): GET https://api.search.brave.com/res/v1/web/search
with X-Subscription-Token <key>; response
{type, query, web: {results: [{url, title, description, ...}]}, ...}.
Description snippets carry inline HTML (<strong>) — cleaned in base.snippet().
"""
from __future__ import annotations

import httpx

from .base import Provider, ProviderError, Result


class Brave(Provider):
    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    @property
    def configured(self) -> bool:
        return bool(self.s.BRAVE_API_KEY)

    async def search(self, query: str, num: int) -> list[Result]:
        headers = {
            "X-Subscription-Token": self.s.BRAVE_API_KEY,
            "Accept": "application/json",
        }
        params = {"q": query, "count": max(1, num)}
        try:
            r = await self.client.get(self.ENDPOINT, params=params, headers=headers)
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
        for item in (data.get("web") or {}).get("results", []):
            url = (item.get("url") or "").strip()
            if not url:
                continue
            out.append(
                Result(
                    url=url,
                    title=self.clean_html(item.get("title") or url),
                    snippet=self.snippet(item.get("description") or ""),
                    score=None,
                )
            )
        return out
