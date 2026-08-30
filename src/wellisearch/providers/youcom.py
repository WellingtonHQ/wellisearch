"""You.com Search API — web-scale search provider.

Verified live (2026-08): POST https://api.you.com/v1/search with
X-API-Key <key>; body {"query", "count"}; response
{results: {web: [{url, title, description, favicon_url, snippets}]}, metadata}.
No relevance score in the response — score stays None (like Brave).
"""
from __future__ import annotations

import httpx

from .base import Provider, ProviderError, Result


class YouCom(Provider):
    name = "youcom"
    ENDPOINT = "https://api.you.com/v1/search"

    @property
    def configured(self) -> bool:
        return bool(self.s.YOUCOM_API_KEY)

    async def search(
        self,
        query: str,
        num: int,
    ) -> list[Result]:
        headers = {"X-API-Key": self.s.YOUCOM_API_KEY}
        body = {"query": query, "count": max(1, num)}
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
        for item in (data.get("results") or {}).get("web", []):
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
