"""GenericExtractor: the fallback extractor for unknown domains (design §3.2).

trafilatura first, readability-lxml → markdownify as the fallback; the
quality gate is a minimum markdown length (MIN_MD_CHARS). Also hosts the
shared fit-markdown helpers (generic_md, trim_md, cut_at_first) that the
site extractors build on.
"""
from __future__ import annotations

import re

from ...config import get_settings
from ..results import Fitted, Rendered

MIN_MD_CHARS = 200
# News article body gate (ap/guardian/reuters): a real article body clears this;
# nav/decoy/related-links stubs do not.
MIN_ARTICLE_BODY_CHARS = 1000
# Retail product pages (buy-box + "About this item" + specs) are 5k+ chars once
# extracted; a thin/degraded render (e.g. the HTTP tier's lazy buy-box) is well
# under this. The gate uses it to reject thin renders so the engine escalates
# to the browser tier instead of accepting a stub (design §3.2 retail gate).
MIN_PRODUCT_CHARS = 3000


class GenericExtractor:
    """Fallback extractor: fit-markdown from any page (the 'generic IE')."""

    name = "generic"

    def fit(self, r: Rendered) -> Fitted:
        """Extract markdown (trafilatura, else readability) + title; never raises."""
        md = generic_md(r.html)
        title = r.title
        if title is None:
            title = _trafilatura_title(r.html)
        return Fitted(md=md, title=title, flags={"extractor": "generic"})

    def accept(self, f: Fitted) -> bool:
        """Gate: markdown must clear the minimum length."""
        return len(f.md.strip()) >= MIN_MD_CHARS


def generic_md(html: str) -> str:
    """Hybrid fit-markdown: trafilatura, else readability; '' on total failure."""
    try:
        from trafilatura import extract

        md = extract(html, output_format="markdown", include_links=False, include_comments=False)
        if md:
            return md
    except Exception:
        pass
    return _fallback_md(html)


def trim_md(md: str) -> str:
    """Strip >80-char whitespace runs, collapse 4+ newlines, cap at CRAWL_MD_MAX_CHARS."""
    md = re.sub(r"[ \t]{80,}", " ", md)
    md = re.sub(r"\n{4,}", "\n\n", md)
    return md[: get_settings().CRAWL_MD_MAX_CHARS]


def cut_at_first(md: str, markers: tuple[str, ...]) -> str:
    """Keep text before the first marker occurrence (case-insensitive); all if none."""
    low = md.lower()
    cut = len(md)
    for marker in markers:
        i = low.find(marker.lower())
        if i != -1 and i < cut:
            cut = i
    return md[:cut]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trafilatura_title(html: str) -> str | None:
    """Best-effort page title via trafilatura metadata; None on any failure."""
    try:
        from trafilatura import metadata

        meta = metadata(html)
        return meta.get("title")
    except Exception:
        return None


def _fallback_md(html: str) -> str:
    """readability-lxml → markdownify; '' on any failure (broken html never raises)."""
    try:
        from readability import Document
        from markdownify import markdownify as mdify

        doc = Document(html)
        return mdify(doc.content, heading_style="ATX") or ""
    except Exception:
        return ""
