"""EXA Search API — AI-native semantic search provider.

Verified live (2026-08): POST https://api.exa.ai/search with
x-api-key <key>; body {"query", "numResults", "contents": {"text": true}};
response {requestId, results: [{id, url, title, publishedDate?, text?}], ...}.
No relevance score in the response — score stays None (like Brave).
"""
from __future__ import annotations

import httpx

from .base import Provider, ProviderError, Result


class Exa(Provider):
    name = "exa"
    ENDPOINT = "https://api.exa.ai/search"

    @property
    def configured(self) -> bool:
        return bool(self.s.EXA_API_KEY)

    async def search(self, query: str, num: int) -> list[Result]:
        headers = {"x-api-key": self.s.EXA_API_KEY}
        body = {
            "query": query,
            "numResults": max(1, num),
            # ask for page text so we have a snippet; base.snippet() trims it
            "contents": {"text": True, "maxChars": 800},
        }
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
                    snippet=self.snippet(item.get("text") or ""),
                    score=None,
                )
            )
        return out
