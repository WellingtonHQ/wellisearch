"""WalmartExtractor: JSON-LD ProductGroup extraction (design §3.2).

walmart.ca product pages carry everything in a single application/ld+json
block — a ProductGroup node with name, description, aggregateRating, and a
`review` array of full review bodies (author, date, stars), plus hasVariant[0]
holding sku/gtin/model/brand/color and the CAD offer. The extractor anchors on
that node rather than generic content extraction: trafilatura extracts only a
thin price/delivery slice from the site's heavy client-rendered HTML.

walmart.com pages carry no Product JSON-LD, so they keep the legacy generic
fit-markdown + hero-price gate (price signal and MIN_PRODUCT_CHARS).

The structured gate requires title + price: a real product page has both; an
Akamai/CF challenge page or a degraded render has neither.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from ...config import get_settings
from ..results import Fitted, Rendered
from ..signals import find_price, find_stock
from . import register
from .base import MIN_PRODUCT_CHARS, generic_md, trim_md


class WalmartExtractor:
    """Walmart product page: JSON-LD ProductGroup (walmart.ca) or legacy generic fit."""

    name = "walmart"

    def fit(self, r: Rendered) -> Fitted:
        """Fit a walmart.com/walmart.ca product page into structured markdown."""
        node = _product_node(r.html)
        if node is not None:
            return self._fit_structured(node, r)
        # No ProductGroup/Product JSON-LD (walmart.com pages, degraded renders):
        # legacy generic fit-markdown + price/stock signals.
        return Fitted(
            md=trim_md(generic_md(r.html)),
            title=r.title,
            signals={"price": find_price(r.html), "stock": find_stock(r.html)},
            flags={"extractor": "walmart"},
        )

    def _fit_structured(self, node: dict, r: Rendered) -> Fitted:
        """Structured markdown from the JSON-LD product node."""
        limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
        variant = _variant(node)
        title = _title(node, r)
        price = _price(variant)
        rating, rating_count = _rating(node)
        brand = _brand(variant)
        description = _description(node, variant)
        details = _details(variant)
        reported = _reported_review_count(node)
        reviews = _reviews(node, limit)

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if price:
            parts.append(f"**Price:** {price}")
        if rating or rating_count:
            bits = [b for b in (rating, rating_count) if b]
            parts.append(f"**Rating:** {' '.join(bits)}")
        if brand:
            parts.append(f"**Brand:** {brand}")
        if description:
            parts.append("## Product description")
            parts.append(description)
        if details:
            parts.append("## Product details")
            parts.extend(f"- {d}" for d in details)
        # Product pages always carry the reviews heading (even with zero
        # embedded reviews) so needs_refresh has a stable marker to test.
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
                "rating": rating,
                "brand": brand,
                "description": len(description or ""),
                "details": len(details),
                "reviews": len(reviews),
                "reviews_reported": reported or 0,
            },
            flags={"extractor": "walmart", "structured": True},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: structured fit needs title + price; legacy fit needs a price and real content."""
        if f.flags.get("structured"):
            return bool(f.title) and bool(f.signals.get("price"))
        return bool(f.signals.get("price")) and len(f.md.strip()) >= MIN_PRODUCT_CHARS


def needs_refresh(url: str, md: str) -> bool:
    """Recrawl walmart.ca product pages stored before reviews were extracted (or at an older limit)."""
    if not is_product_url(url):
        return False
    host = (urlparse(url).hostname or "").lower()
    # walmart.com pages keep the legacy generic fit and never carry the reviews
    # heading; without this check every stored .com page would re-crawl forever.
    if host not in ("walmart.ca", "www.walmart.ca"):
        return False
    limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
    return _reviews_heading(limit) not in md


def is_product_url(url: str) -> bool:
    """True for a walmart.com/walmart.ca product detail page (/ip/... or /en/ip/...)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("walmart.com", "www.walmart.com", "walmart.ca", "www.walmart.ca"):
        return False
    return _PRODUCT_PATH_RE.search(parsed.path) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# (key, label) pairs rendered as "- Label: value" lines under Product details,
# in display order.
_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("model", "Model"),
    ("sku", "SKU"),
    ("gtin13", "GTIN"),
    ("color", "Color"),
)
_PRODUCT_PATH_RE = re.compile(r"/(?:en/)?ip/[^/?#]+/\d+")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _product_node(html: str) -> dict | None:
    """The first JSON-LD node of @type ProductGroup or Product with a name, else None."""
    for script in _LD_JSON_RE.findall(html):
        try:
            parsed = json.loads(script)
        except ValueError:
            continue
        found = _find_product(parsed)
        if found is not None:
            return found
    return None


def _find_product(node: object) -> dict | None:
    """A product node, searching plain lists and @graph arrays."""
    if isinstance(node, list):
        for item in node:
            found = _find_product(item)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    t = node.get("@type")
    types = [t] if isinstance(t, str) else (list(t) if isinstance(t, list) else [])
    if any(x in ("Product", "ProductGroup") for x in types) and _name_of(node):
        return node
    for item in node.get("@graph") or []:
        found = _find_product(item)
        if found is not None:
            return found
    return None


def _name_of(node: dict) -> str | None:
    """The node's name field, stripped; None when absent or blank."""
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _variant(node: dict) -> dict:
    """The concrete variant carrying offers/specs (first hasVariant entry, else the node itself)."""
    variants = node.get("hasVariant")
    items = variants if isinstance(variants, list) else [variants]
    for v in items:
        if isinstance(v, dict):
            return v
    return node


def _title(node: dict, r: Rendered) -> str | None:
    """Product name from the JSON-LD node, falling back to the page title."""
    return _name_of(node) or r.title


def _price(variant: dict) -> str | None:
    """First real offer price formatted with its currency, else None."""
    offers = variant.get("offers")
    items = offers if isinstance(offers, list) else [offers]
    for offer in items:
        if not isinstance(offer, dict):
            continue
        text = _format_price(offer.get("price"), str(offer.get("priceCurrency") or "USD"))
        if text is not None:
            return text
    return None


def _format_price(price: object, currency: str) -> str | None:
    """A display price (e.g. $88.00), else None for a missing or non-positive price."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    symbol = "$" if currency in ("USD", "CAD") else f"{currency} "
    return f"{symbol}{value:,.2f}"


def _rating(node: dict) -> tuple[str | None, str | None]:
    """(star rating text, reported-count text) from aggregateRating, else (None, None)."""
    agg = node.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    return _star_text(agg.get("ratingValue")), _count_text(agg.get("reviewCount") or agg.get("ratingCount"))


def _star_text(value: object) -> str | None:
    """'4.1 out of 5 stars' for a valid rating value, else None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 5:
        return None
    return f"{v:g} out of 5 stars"


def _count_text(value: object) -> str | None:
    """'2,704 ratings' for a valid count, else None."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{n:,} ratings"


def _reported_review_count(node: dict) -> int | None:
    """Walmart's total review count, or None when it is missing or invalid."""
    agg = node.get("aggregateRating")
    if not isinstance(agg, dict):
        return None
    raw = agg.get("reviewCount") or agg.get("ratingCount")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _brand(variant: dict) -> str | None:
    """Brand name from the variant's brand object (or a plain string brand)."""
    brand = variant.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def _description(node: dict, variant: dict) -> str | None:
    """Product description text from the node (fallback: the variant), tags stripped."""
    for source in (node, variant):
        desc = source.get("description")
        if isinstance(desc, str) and desc.strip():
            return _strip_tags(desc)
    return None


def _details(variant: dict) -> list[str]:
    """'- Label: value' lines for the known spec fields, in display order."""
    out: list[str] = []
    for key, label in _DETAIL_FIELDS:
        value = variant.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            out.append(f"{label}: {value}")
    return out


def _reviews_heading(limit: int) -> str:
    """A readable heading that also marks the indexed review limit."""
    return f"## Walmart reviews (up to {limit})"


def _reviews(node: dict, limit: int) -> list[str]:
    """Review entries from the node's `review` array, capped at limit."""
    reviews = node.get("review")
    if not isinstance(reviews, list):
        return []
    out: list[str] = []
    for review in reviews:
        if not isinstance(review, dict):
            continue
        body = _review_body(review)
        if not body:
            continue
        label = f"### {len(out) + 1}. {_review_author(review) or 'unknown'}"
        stars = _review_stars(review)
        if stars:
            label += f" ({stars})"
        block: list[str] = [label]
        headline = review.get("name")
        date = review.get("datePublished")
        if isinstance(headline, str) and headline.strip():
            line = f"**{headline.strip()}**"
            if isinstance(date, str) and date.strip():
                line += f" — {date.strip()}"
            block.append(line)
        elif isinstance(date, str) and date.strip():
            block.append(date.strip())
        block.append(body)
        out.append("\n\n".join(block))
        if len(out) >= limit:
            break
    return out


def _review_author(review: dict) -> str | None:
    """Reviewer name from the author object (or a plain string author)."""
    author = review.get("author")
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(author, str) and author.strip():
        return author.strip()
    return None


def _review_stars(review: dict) -> str | None:
    """'N out of 5 stars' from the reviewRating value, else None."""
    rating = review.get("reviewRating")
    if not isinstance(rating, dict):
        return None
    return _star_text(rating.get("ratingValue"))


def _review_body(review: dict) -> str | None:
    """Review body text with tags stripped and blank runs collapsed."""
    body = review.get("reviewBody")
    if not isinstance(body, str):
        return None
    text = re.sub(r"\n{3,}", "\n\n", _strip_tags(body))
    return text or None


def _strip_tags(text: str) -> str:
    """Visible text: drop tags and collapse whitespace runs (newlines kept)."""
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


_TAG_RE = re.compile(r"<[^>]+>")

register(WalmartExtractor(), "walmart.com")
register(WalmartExtractor(), "walmart.ca")
