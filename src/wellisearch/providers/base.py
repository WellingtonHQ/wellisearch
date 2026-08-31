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

import httpx

from ..config import Settings

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class Result:
    """Canonical search result: url, title, snippet, optional score, and
    provider-specific extras."""
    url: str
    title: str
    snippet: str = ""
    score: float | None = None
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """A provider call failed (auth, quota, network, 5xx…). Gateway fails over."""

    def __init__(
        self,
        provider: str,
        message: str,
        status: int | None = None,
    ) -> None:
        """Keep the provider name and optional HTTP status on the failure."""
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class Provider:
    """Interface: name + configured flag + async search()."""

    name: str = "base"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
    ) -> None:
        """Keep the settings and the shared HTTP client."""
        self.s = settings
        self.client = client

    @property
    def configured(self) -> bool:
        """Whether the provider is usable (has its key/url); base is always True."""
        return True

    async def search(
        self,
        query: str,
        num: int,
    ) -> list[Result]:
        """Search the provider and return canonical Results; adapters must implement."""
        raise NotImplementedError

    # ---------------------------------------------------------------------------
    # Utilities
    # ---------------------------------------------------------------------------

    @staticmethod
    def clean_html(text: str) -> str:
        """Strip inline HTML (Brave snippets carry <strong> etc.)."""
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
