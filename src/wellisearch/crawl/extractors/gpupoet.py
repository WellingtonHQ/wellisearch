"""GpupoetExtractor: GPU marketplace listing cards (design §3.2).

Anchors on the Bootstrap card grid (div.card.m-1) that gpupoet.com renders
server-side. Each listing card carries the GPU title, price, condition,
country, and price-per-TFLOPs as badge spans, plus a marketplace redirect
link in the card footer.

The generic extractor (trafilatura/readability) fails on this page because
the Suspense "Loading..." placeholder and the filter sidebar dominate the
DOM, so the listing cards are never surfaced. This extractor skips straight
to the card grid.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from ..results import Fitted, Rendered
from . import register
from .base import trim_md


class GpupoetExtractor:
    """GPU Poet listing page: card-anchored extraction + site gate."""

    name = "gpupoet"

    def fit(self, r: Rendered) -> Fitted:
        """Fit a GPU Poet listing page into structured markdown."""
        soup = _soup(r.html)
        title = r.title or _page_title(soup)
        listings = _listing_cards(soup)

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if listings:
            parts.append(f"{len(listings)} active listings.")
            for i, ls in enumerate(listings, 1):
                parts.append(f"{i}. **{ls['name']}** — {ls['price']}"
                             f" · {ls['condition']} · {ls['country']}"
                             f" · {ls['value']}"
                             f" · [{ls['marketplace']}]({ls['link']})")

        md = trim_md("\n\n".join(parts))
        return Fitted(
            md=md,
            title=title,
            signals={"listings": len(listings)},
            flags={"extractor": "gpupoet"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: at least one listing card with a price (real listing content)."""
        return int(f.signals.get("listings", 0)) >= 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _soup(html: str) -> BeautifulSoup:
    """Parse HTML into a BeautifulSoup tree."""
    return BeautifulSoup(html, "lxml")


def _page_title(soup: BeautifulSoup) -> str | None:
    """Page title from <h1>, falling back to <title>."""
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    if soup.title:
        return soup.title.get_text(strip=True)
    return None


def _listing_cards(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Extract listing data from div.card.m-1 elements (excluding filter sidebar)."""
    cards = soup.select("div.card.m-1")
    out: list[dict[str, str]] = []
    for card in cards:
        if "Filter Listings" in card.get_text(" ", strip=True):
            continue
        out.append(_parse_card(card))
    return out


def _parse_card(card: BeautifulSoup) -> dict[str, str]:
    """Parse one listing card into a flat dict of fields."""
    name = _card_name(card)
    badges = card.select("div.card-text span.badge.rounded-pill")
    price, condition, country, value = _badges(badges)
    marketplace, link = _marketplace(card)
    return {
        "name": name,
        "price": price,
        "condition": condition,
        "country": country,
        "value": value,
        "marketplace": marketplace,
        "link": link,
    }


def _card_name(card: BeautifulSoup) -> str:
    """Listing name from h5.card-title."""
    el = card.select_one("h5.card-title")
    if el:
        return el.get_text(" ", strip=True)
    return "(unnamed)"


def _badges(badges: list[BeautifulSoup]) -> tuple[str, str, str, str]:
    """Split badge spans into (price, condition, country, value)."""
    price, condition, country, value = "", "", "", ""
    for b in badges:
        text = b.get_text(" ", strip=True)
        if not text:
            continue
        if "text-bg-primary" in (b.get("class") or []):
            value = text
        elif text.startswith("$"):
            price = text
        elif _is_country(b):
            country = _country_name(b)
        else:
            condition = text
    return price, condition, country, value


def _is_country(b: BeautifulSoup) -> bool:
    """True when the badge wraps an <abbr> with a title (country code)."""
    return b.find("abbr", title=True) is not None


def _country_name(b: BeautifulSoup) -> str:
    """Country name from the <abbr title> attribute, else the badge text."""
    abbr = b.find("abbr", title=True)
    if abbr:
        return abbr["title"]
    return b.get_text(" ", strip=True)


def _marketplace(card: BeautifulSoup) -> tuple[str, str]:
    """Marketplace name + decoded URL from the card footer link."""
    a = card.select_one("div.card-footer a")
    if not a:
        return "", ""
    name = _marketplace_name(a)
    link = _decode_href(a.get("href", ""))
    return name, link


def _marketplace_name(a: BeautifulSoup) -> str:
    """Marketplace name from the SVG aria-label, else a generic label."""
    svg = a.find("svg", attrs={"aria-label": True})
    if svg:
        return svg["aria-label"]
    return "Link"


def _decode_href(href: str) -> str:
    """Decode a /bye?to=<url> redirect to the target URL."""
    if not href:
        return ""
    if href.startswith("/bye?"):
        qs = parse_qs(urlparse(href).query)
        target = qs.get("to", [""])[0]
        return unquote(target)
    return href


register(GpupoetExtractor(), "gpupoet.com")
