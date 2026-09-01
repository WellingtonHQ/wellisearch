"""WSJExtractor: paywall-stub flag — paywall is content, not engine (design §3.2)."""
from __future__ import annotations

from ..results import Fitted, Rendered
from . import register
from .base import generic_md, trim_md

PAYWALL_MARKERS = ("paywall", "subscribe to read", "sign in to continue", "metered")


class WSJExtractor:
    """WSJ article: detect the paywall stub and flag it, don't escalate."""

    name = "wsj"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown + paywall flag (a stub is acceptable content)."""
        paywall = any(marker in r.html.lower() for marker in PAYWALL_MARKERS)
        return Fitted(
            md=trim_md(generic_md(r.html)),
            title=r.title,
            signals={},
            flags={"extractor": "wsj", "paywall": paywall},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a lead paragraph is enough (a paywall stub is acceptable)."""
        return len(f.md.strip()) >= 500


register(WSJExtractor(), "wsj.com")
