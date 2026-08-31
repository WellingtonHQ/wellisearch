"""NYTimesExtractor: full-body gate, stealth escalation on paywall stubs (design §3.2)."""
from __future__ import annotations

from ..results import Escalate, Fitted, Rendered
from . import register
from .base import generic_md, trim_md

MIN_ARTICLE_CHARS = 2000  # metered-paywall stub is ~1.8k chars; full articles 11k+


class NYTimesExtractor:
    """NYT article: escalate to stealth when the body is a metered-paywall stub."""

    name = "nytimes"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown; raise Escalate('stealth') when the body is too thin."""
        base = generic_md(r.html)
        if len(base) < MIN_ARTICLE_CHARS:
            raise Escalate("stealth")
        return Fitted(
            md=trim_md(base),
            title=r.title,
            signals={},
            flags={"extractor": "nytimes"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a full article body, not a paywall stub."""
        return len(f.md.strip()) >= MIN_ARTICLE_CHARS


register(NYTimesExtractor(), "nytimes.com")
