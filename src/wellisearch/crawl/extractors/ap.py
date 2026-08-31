"""APExtractor: hybrid fit-markdown + head-decoy trim (design §3.2)."""
from __future__ import annotations

from ..results import Fitted, Rendered
from . import register
from .base import generic_md, trim_md

HEAD_DECOYS = ("AP News", "Most Popular", "Newsletters", "Sign up")
MIN_REAL_PARA_CHARS = 80  # a real article paragraph, not a nav/decoy line


class APExtractor:
    """AP article: drop head-decoy text that precedes the first real paragraph."""

    name = "ap"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown with the head-decoy block dropped when present."""
        return Fitted(
            md=trim_md(_drop_head_decoys(generic_md(r.html))),
            title=r.title,
            signals={},
            flags={"extractor": "ap"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a real article body."""
        return len(f.md.strip()) >= 1000


def _drop_head_decoys(md: str) -> str:
    """Drop text before the first real paragraph when a head-decoy marker precedes it."""
    low = md.lower()
    marker_pos = len(md)
    for marker in HEAD_DECOYS:
        i = low.find(marker.lower())
        if i != -1 and i < marker_pos:
            marker_pos = i
    real_pos = _first_real_paragraph(md)
    if real_pos is None or marker_pos > real_pos:
        return md
    return md[real_pos:]


def _first_real_paragraph(md: str) -> int | None:
    """Offset of the first MIN_REAL_PARA_CHARS+ paragraph, else None."""
    start = 0
    for para in md.split("\n\n"):
        if len(para.strip()) >= MIN_REAL_PARA_CHARS:
            return start
        start += len(para) + 2
    return None


register(APExtractor(), "apnews.com")
