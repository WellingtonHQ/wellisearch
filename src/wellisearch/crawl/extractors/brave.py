"""BraveExtractor: Brave Search SERP pages — SPA visible-text fallback (design §3.2).

search.brave.com is a SvelteKit single-page app. Before client hydration the DOM
is an empty shell and trafilatura finds nothing; once hydrated, the result
markup carries per-build CSS class names with no stable selectors to anchor on.
fit() therefore tries generic_md first (it handles fully-hydrated results well)
and falls back to a visible-text markdown of the body when that comes up thin —
which keeps SPA SERPs clearable by the gate without site-specific selectors.
"""
from __future__ import annotations

from ..results import Fitted, Rendered
from . import register
from .base import generic_md, MIN_MD_CHARS, trim_md


class BraveExtractor:
    """Brave Search SERP page: hybrid markdown with a visible-text fallback."""

    name = "brave"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown; when trafilatura yields too little (SPA shell), fall back to the body's visible text."""
        md = generic_md(r.html)
        flags: dict[str, bool] = {"extractor": "brave"}
        if len(md.strip()) < MIN_MD_CHARS:
            fallback = _visible_text_markdown(r.html)
            if len(fallback.strip()) > len(md.strip()):
                md = fallback
                flags["spa_fallback"] = True
        return Fitted(
            md=trim_md(md),
            title=r.title,
            flags=flags,
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: markdown must clear the minimum length."""
        return len(f.md.strip()) >= MIN_MD_CHARS


def _visible_text_markdown(html: str) -> str:
    """The body's visible text as one line per terminal element; '' on any failure.

    SPA fallback for DOMs where trafilatura/readability find no content area:
    strips scripts/styles/noscripts, then emits each element that contains only
    inline children (no nested block elements) as one whitespace-collapsed
    line in document order, so container text is never counted twice and
    repeated nav labels dedupe.
    """
    try:
        return "\n".join(_visible_text_lines(html))
    except Exception:
        return ""


def _visible_text_lines(html: str) -> list[str]:
    """Deduped visible-text lines from the body's terminal elements."""
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    root = soup.body or soup
    lines: list[str] = []
    seen: set[str] = set()
    for el in root.find_all(True):
        if el.find(True) is not None:
            continue  # has a nested element — its text is emitted deeper down
        text = " ".join(el.get_text().split())
        if text and text not in seen:
            seen.add(text)
            lines.append(text)
    return lines


def _soup(html: str) -> BeautifulSoup:
    """BeautifulSoup (lxml) parse; the repo's shared parser choice."""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml")


register(BraveExtractor(), "search.brave.com")
