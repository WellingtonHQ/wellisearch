"""AmazonExtractor: element-anchored extraction (design §3.2).

Anchors on known Amazon structure rather than generic content extraction:
  - title      → #productTitle (fallback <h1>)
  - price      → buy-box .a-price .a-offscreen (first real value)
  - stock      → #availability
  - rating     → #acrPopover title attr + #acrCustomerReviewText count
  - seller     → #sellerProfileTriggerId (buy-box "Sold by")
  - bullets    → #feature-bullets ("About this item")
  - description→ #productDescription ("Product description")
  - details    → product-details table (tech-spec / detail-bullets)
  - reviews    → [data-hook="review"] cards ("Top reviews", displayed order)

The gate requires title + price + at least one feature bullet, which is a
stronger, site-specific signal than a raw char count: it accepts a real
product page and rejects a thin/degraded render or a decoy section
("Add to your order", "Frequently bought together") that has no bullets.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ...config import get_settings
from ..results import Fitted, Rendered
from . import register
from .base import trim_md


class AmazonExtractor:
    """Amazon product page: element-anchored extraction + site gate."""

    name = "amazon"

    def fit(self, r: Rendered) -> Fitted:
        """Fit an Amazon product page into structured markdown."""
        soup = _soup(r.html)
        limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
        title = _title(soup)
        price = _price(soup)
        stock = _stock(soup)
        rating = _rating(soup)
        review_count = _review_count(soup)
        seller = _seller(soup)
        bullets = _feature_bullets(soup)
        description = _description(soup)
        details = _product_details(soup)
        reported = _reported_review_count(soup)
        reviews = _reviews(soup, limit)

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if price:
            line = f"**Price:** {price}"
            if stock:
                line += f" — {stock}"
            parts.append(line)
        if rating or review_count:
            bits = [b for b in (rating, review_count) if b]
            parts.append(f"**Rating:** {' '.join(bits)}")
        if seller:
            parts.append(f"**Sold by:** {seller}")
        if bullets:
            parts.append("## About this item")
            parts.extend(f"- {b}" for b in bullets)
        if description:
            parts.append("## Product description")
            parts.append(description)
        if details:
            parts.append("## Product details")
            parts.append(details)
        # Product pages always carry the reviews heading (even with zero
        # rendered cards) so needs_refresh has a stable marker to test.
        if bullets or reported is not None or reviews:
            parts.append(_reviews_heading(limit))
            if reviews:
                parts.extend(reviews)
            else:
                parts.append("No reviews available.")

        md = trim_md("\n\n".join(parts))
        return Fitted(
            md=md,
            title=title or r.title,
            signals={
                "price": price,
                "stock": stock,
                "rating": rating,
                "seller": seller,
                "bullets": len(bullets),
                "description": len(description or ""),
                "reviews": len(reviews),
                "reviews_reported": reported or 0,
            },
            flags={"extractor": "amazon"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: title + price + at least one feature bullet (real product content)."""
        return (
            bool(f.title)
            and bool(f.signals.get("price"))
            and int(f.signals.get("bullets", 0)) >= 1
        )


def needs_refresh(url: str, md: str) -> bool:
    """Recrawl product pages stored before reviews were extracted (or at an older limit)."""
    if not is_product_url(url):
        return False
    limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
    return _reviews_heading(limit) not in md


def is_product_url(url: str) -> bool:
    """True for an amazon.com product detail page (/<slug>/dp/<ASIN> or /dp/<ASIN>)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "amazon.com" and not host.endswith(".amazon.com"):
        return False
    return _PRODUCT_PATH_RE.search(parsed.path) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
_PRODUCT_PATH_RE = re.compile(r"/dp/[A-Z0-9]{10}(?=/|$)", re.IGNORECASE)


def _soup(html: str) -> BeautifulSoup:
    """Parse HTML into a BeautifulSoup tree."""
    return BeautifulSoup(html, "lxml")


def _title(soup: BeautifulSoup) -> str | None:
    """Product title from #productTitle, falling back to <h1>."""
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
    """First real price found by the buy-box selectors."""
    for sel in _PRICE_SELECTORS:
        for e in soup.select(sel):
            t = e.get_text(strip=True)
            if t and t.lower() != "null":
                return t
    return None


def _stock(soup: BeautifulSoup) -> str | None:
    """Availability text from #availability."""
    el = soup.find(id="availability")
    if el:
        t = el.get_text(" ", strip=True)
        if t:
            return t
    return None


def _feature_bullets(soup: BeautifulSoup) -> list[str]:
    """Feature bullets from #feature-bullets."""
    fb = soup.find(id="feature-bullets")
    if not fb:
        return []
    out: list[str] = []
    for li in fb.select("li"):
        t = li.get_text(" ", strip=True)
        if t:
            out.append(t)
    return out


def _description(soup: BeautifulSoup) -> str | None:
    """Product description paragraphs from #productDescription (fallback #product-description)."""
    el = soup.find(id="productDescription") or soup.find(id="product-description")
    if not el:
        return None
    paras = [p.get_text(" ", strip=True) for p in el.select("p")]
    if not paras:
        t = el.get_text(" ", strip=True)
        return t or None
    return "\n\n".join(p for p in paras if p)


def _product_details(soup: BeautifulSoup) -> str | None:
    """First product-details section text from the known section IDs."""
    for tid in _DETAILS_IDS:
        el = soup.find(id=tid)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return None


def _rating(soup: BeautifulSoup) -> str | None:
    """Star rating from #acrPopover's title attr, else the .a-icon-alt span."""
    el = soup.find(id="acrPopover")
    if el is not None and el.get("title"):
        t = el["title"].strip()
        if t:
            return t
    icon = soup.select_one("#averageCustomerReviews .a-icon-alt")
    if icon is not None:
        t = icon.get_text(strip=True)
        if t:
            return t
    return None


def _review_count(soup: BeautifulSoup) -> str | None:
    """Review/rating count text from #acrCustomerReviewText."""
    el = soup.find(id="acrCustomerReviewText")
    if el is not None:
        t = el.get_text(strip=True)
        if t:
            return t
    return None


def _reported_review_count(soup: BeautifulSoup) -> int | None:
    """Amazon's total review count, or None when it is missing or invalid."""
    el = soup.find(id="acrCustomerReviewText")
    if el is None:
        return None
    m = re.search(r"\d[\d,]*", el.get_text())
    if not m:
        return None
    return int(m.group().replace(",", ""))


def _seller(soup: BeautifulSoup) -> str | None:
    """Buy-box "Sold by" seller from #sellerProfileTriggerId."""
    el = soup.find(id="sellerProfileTriggerId")
    if el is not None:
        t = el.get_text(strip=True)
        if t:
            return t
    return None


def _reviews_heading(limit: int) -> str:
    """A readable heading that also marks the indexed review limit."""
    return f"## Amazon reviews (up to {limit})"


def _reviews(soup: BeautifulSoup, limit: int) -> list[str]:
    """Review cards in Amazon's displayed top-reviews order, capped at limit."""
    out: list[str] = []
    for card in soup.select('[data-hook="review"]'):
        body = _review_body(card)
        if not body:
            continue
        label = f"### {len(out) + 1}. {_review_author(card) or 'unknown'}"
        details = _review_details(card)
        if details:
            label += f" ({', '.join(details)})"
        block: list[str] = [label]
        title = _review_title(card)
        date = _review_date(card)
        if title:
            block.append(f"**{title}**" + (f" — {date}" if date else ""))
        elif date:
            block.append(date)
        block.append(body)
        out.append("\n\n".join(block))
        if len(out) >= limit:
            break
    return out


def _review_details(card: Tag) -> list[str]:
    """Rating + verified-purchase markers for a review label line."""
    out: list[str] = []
    stars = _review_stars(card)
    if stars:
        out.append(stars)
    if _review_verified(card):
        out.append("verified purchase")
    return out


def _review_author(card: Tag) -> str | None:
    """Reviewer display name from .a-profile-name."""
    el = card.select_one(".a-profile-name")
    return el.get_text(strip=True) if el else None


def _review_stars(card: Tag) -> str | None:
    """Star rating from the review-star-rating icon, else its a-star-N class."""
    icon = card.select_one('[data-hook="review-star-rating"] .a-icon-alt')
    if icon is not None:
        t = icon.get_text(strip=True)
        if t:
            return t
    star = card.select_one('[data-hook="review-star-rating"]')
    if star is not None:
        m = re.search(r"a-star-(\d)", " ".join(star.get("class") or []))
        if m:
            return f"{m.group(1)} out of 5 stars"
    return None


def _review_title(card: Tag) -> str | None:
    """Review headline from [data-hook=reviewTitle]."""
    el = card.select_one('[data-hook="reviewTitle"]')
    return el.get_text(strip=True) if el else None


def _review_date(card: Tag) -> str | None:
    """Review date line from [data-hook=review-date]."""
    el = card.select_one('[data-hook="review-date"]')
    return el.get_text(" ", strip=True) if el else None


def _review_verified(card: Tag) -> bool:
    """True when the review carries the Verified Purchase badge."""
    return card.select_one('[data-hook="avp-badge"]') is not None


def _review_body(card: Tag) -> str | None:
    """Review body paragraphs from the rich-content container (reviewText fallback)."""
    el = (
        card.select_one('[data-hook="reviewRichContentContainer"]')
        or card.select_one('[data-hook="reviewText"]')
    )
    if not el:
        return None
    paras = [p.get_text(" ", strip=True) for p in el.select("p")]
    if not paras:
        t = el.get_text(" ", strip=True)
        return t or None
    return "\n\n".join(p for p in paras if p)


register(AmazonExtractor(), "amazon.com")
