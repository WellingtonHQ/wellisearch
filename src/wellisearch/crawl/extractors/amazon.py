"""AmazonExtractor: element-anchored extraction (design §3.2).

Anchors on known Amazon structure rather than generic content extraction:
  - title   → #productTitle (fallback <h1>)
  - price   → buy-box .a-price .a-offscreen (first real value)
  - stock   → #availability
  - bullets → #feature-bullets ("About this item")
  - details → product-details table (tech-spec / detail-bullets)

The gate requires title + price + at least one feature bullet, which is a
stronger, site-specific signal than a raw char count: it accepts a real
product page and rejects a thin/degraded render or a decoy section
("Add to your order", "Frequently bought together") that has no bullets.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..results import Fitted, Rendered
from . import register
from .base import trim_md

_PRICE_SELECTORS = (
    "[data-asin] .a-price .a-offscreen",
    ".a-price .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
)
_DETAILS_IDS = (
    "productDetails_techSpec_section_1",
    "productDetails_detailBullets_sections1",
    "detailBullets_feature_div",
    "productDetails_db_sections",
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _title(soup: BeautifulSoup) -> str | None:
    el = soup.find(id="productTitle")
    if el:
        t = el.get_text(" ", strip=True)
        if t:
            return t
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    return None


def _price(soup: BeautifulSoup) -> str | None:
    for sel in _PRICE_SELECTORS:
        for e in soup.select(sel):
            t = e.get_text(strip=True)
            if t and t.lower() != "null":
                return t
    return None


def _stock(soup: BeautifulSoup) -> str | None:
    el = soup.find(id="availability")
    if el:
        t = el.get_text(" ", strip=True)
        if t:
            return t
    return None


def _feature_bullets(soup: BeautifulSoup) -> list[str]:
    fb = soup.find(id="feature-bullets")
    if not fb:
        return []
    out: list[str] = []
    for li in fb.select("li"):
        t = li.get_text(" ", strip=True)
        if t:
            out.append(t)
    return out


def _product_details(soup: BeautifulSoup) -> str | None:
    for tid in _DETAILS_IDS:
        el = soup.find(id=tid)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return None


class AmazonExtractor:
    """Amazon product page: element-anchored extraction + site gate."""

    name = "amazon"

    def fit(self, r: Rendered) -> Fitted:
        soup = _soup(r.html)
        title = _title(soup)
        price = _price(soup)
        stock = _stock(soup)
        bullets = _feature_bullets(soup)
        details = _product_details(soup)

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if price:
            line = f"**Price:** {price}"
            if stock:
                line += f" — {stock}"
            parts.append(line)
        if bullets:
            parts.append("## About this item")
            parts.extend(f"- {b}" for b in bullets)
        if details:
            parts.append("## Product details")
            parts.append(details)

        md = trim_md("\n\n".join(parts))
        return Fitted(
            md=md,
            title=title or r.title,
            signals={"price": price, "stock": stock, "bullets": len(bullets)},
            flags={"extractor": "amazon"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: title + price + at least one feature bullet (real product content)."""
        return (
            bool(f.title)
            and bool(f.signals.get("price"))
            and int(f.signals.get("bullets", 0)) >= 1
        )


register(AmazonExtractor(), "amazon.com")
