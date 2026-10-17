"""GuardianExtractor: hybrid fit-markdown + related-links trim (design §3.2)."""
from __future__ import annotations

from .base import MIN_NEWS_ARTICLE_BODY_CHARS, cut_at_first, generic_md, trim_md
from . import register
from ..results import Fitted, Rendered

RELATED = ("Explore more", "Most commented", "Most viewed", "Related stories")


class GuardianExtractor:
    """Guardian article: hard-cut at the first related-stories marker."""

    name = "guardian"

    def fit(self: GuardianExtractor, r: Rendered) -> Fitted:
        """Hybrid markdown cut at the first related marker."""
        base = cut_at_first(generic_md(r.html), RELATED)
        return Fitted(
            md=trim_md(base),
            title=r.title,
            signals={},
            flags={"extractor": "guardian"},
        )

    def accept(self: GuardianExtractor, f: Fitted) -> bool:
        """Gate: a real article body."""
        return len(f.md.strip()) >= MIN_NEWS_ARTICLE_BODY_CHARS


register(GuardianExtractor(), "theguardian.com")
