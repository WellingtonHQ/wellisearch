"""GuardianExtractor: hybrid fit-markdown + related-links trim (design §3.2)."""
from __future__ import annotations

from ..results import Fitted, Rendered
from . import register
from .base import cut_at_first, generic_md, trim_md

RELATED = ("Related stories", "Explore more", "Most viewed", "Most commented")


class GuardianExtractor:
    """Guardian article: hard-cut at the first related-stories marker."""

    name = "guardian"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown cut at the first related marker."""
        base = cut_at_first(generic_md(r.html), RELATED)
        return Fitted(
            md=trim_md(base),
            title=r.title,
            signals={},
            flags={"extractor": "guardian"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a real article body."""
        return len(f.md.strip()) >= 1000


register(GuardianExtractor(), "theguardian.com")
