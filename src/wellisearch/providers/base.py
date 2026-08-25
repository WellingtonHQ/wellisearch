"""Provider base: the single normalization point (plan §7/§15).

Every provider's raw shape is converted here into the canonical Result
contract that the rest of wellisearch (and the LLM) sees. If a provider's
response shape drifts, adapt it in its adapter + these helpers — never
downstream.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class Result:
    url: str
    title: str
    snippet: str = ""
    score: float | None = None
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """A provider call failed (auth, quota, network, 5xx…). Gateway fails over."""

    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class Provider:
    """Interface: name + configured flag + async search()."""

    name: str = "base"

    def __init__(self, settings, client) -> None:
        self.s = settings
        self.client = client

    @property
    def configured(self) -> bool:
        return True

    async def search(self, query: str, num: int) -> list[Result]:
        raise NotImplementedError

    # ------------------------------------------------------------ utilities

    @staticmethod
    def clean_html(text: str) -> str:
        """Strip inline HTML (Brave/SearXNG snippets carry <strong> etc.)."""
        if not text:
            return ""
        text = html.unescape(text or "")
        text = _TAG_RE.sub(" ", text)
        return _WS_RE.sub(" ", text).strip()

    @staticmethod
    def snippet(text: str, limit: int = 400) -> str:
        """Trim to ~limit chars, boundary-safe (never mid-word)."""
        text = Provider.clean_html(text)
        if len(text) <= limit:
            return text
        cut = text[:limit]
        b = cut.rfind(" ")
        if b > limit // 2:
            cut = cut[:b]
        return cut.rstrip(" ,.;:") + "…"
