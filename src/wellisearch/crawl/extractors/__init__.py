"""Extractor registry: content extractors selected by domain (design §3.2).

yt-dlp style: site extractors register under a domain suffix; for_url()
picks the longest-suffix match, falling back to the generic extractor.
"""
from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

from ..results import Fitted, Rendered
from .base import GenericExtractor


class Extractor(Protocol):
    """A content extractor: fit rendered HTML to markdown, gate the result."""

    name: str

    def fit(self, r: Rendered) -> Fitted: ...

    def accept(self, f: Fitted) -> bool: ...


_REGISTRY: dict[str, Extractor] = {}


def register(ex: Extractor, domain: str) -> None:
    """Register an extractor under a domain suffix."""
    _REGISTRY[domain] = ex


def for_url(url: str) -> Extractor:
    """Extractor for a URL: longest-suffix domain match, else generic."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    best: str | None = None
    for domain in _REGISTRY:
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best):
                best = domain
    if best is not None:
        return _REGISTRY[best]
    return GenericExtractor()


# Side-effect imports: each site module registers itself on import.
from . import amazon, ap, bestbuy, guardian, nytimes, reuters, target, walmart, wsj  # noqa: E402,F401
