"""HomeDepotExtractor: element-anchored extraction (design §3.2).

Anchors on the Product node in the page's JSON-LD rather than generic
content extraction: trafilatura extracts only a thin slice of header/nav
text from Home Depot's ~780 KB server HTML, while the application/ld+json
holds everything — name, offers.price, aggregateRating, description,
model/sku/dimension fields, and a `review` array of full review bodies.
The gate requires a Product node with a title and a price, a strong
site-specific signal: an Akamai challenge page or a degraded render has
neither.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from ...config import get_settings
from ..results import Fitted, Rendered
from . import register
from .base import trim_md


class HomeDepotExtractor:
    """Home Depot product page: JSON-LD Product node + site gate."""

    name = "homedepot"

    def fit(self, r: Rendered) -> Fitted:
        """Fit a Home Depot product page into structured markdown."""
        product = _product_node(r.html)
        limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
        title = _title(product, r)
        price = _price(product)
        rating, rating_count = _rating(product)
        brand = _brand(product)
        description = _description(product)
        details = _details(product)
        reported = _reported_review_count(product)
        reviews = _reviews(product, limit)

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
        if product is not None:
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
            flags={"extractor": "homedepot"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a Product node with a title and a price (real product content)."""
        return bool(f.title) and bool(f.signals.get("price"))


def needs_refresh(url: str, md: str) -> bool:
    """Recrawl product pages stored before reviews were extracted (or at an older limit)."""
    if not is_product_url(url):
        return False
    limit = max(1, get_settings().CRAWL_MAX_REVIEWS)
    return _reviews_heading(limit) not in md


def is_product_url(url: str) -> bool:
    """True for a homedepot.com product detail page (/p/<slug>/<sku>), not review pages."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "homedepot.com" and not host.endswith(".homedepot.com"):
        return False
    return _PRODUCT_PATH_RE.match(parsed.path) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# (key, label) pairs rendered as "label: value" lines under Product details,
# in display order (business logic dictates the order, not alphabetical).
_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("model", "Model"),
    ("sku", "SKU"),
    ("gtin13", "GTIN"),
    ("productID", "Internet #"),
    ("color", "Color"),
    ("weight", "Weight"),
    ("width", "Width"),
    ("depth", "Depth"),
    ("height", "Height"),
)
_PRODUCT_PATH_RE = re.compile(r"^/p/[^/]+/\d+")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _product_node(html: str) -> dict | None:
    """The first JSON-LD node of @type Product with a name, else None."""
    for script in _LD_JSON_RE.findall(html):
        node = _find_product(_parse_json(script))
        if node is not None:
            return node
    return None


def _parse_json(raw: str) -> object:
    """Parsed JSON-LD block; None on invalid JSON."""
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _find_product(node: object) -> dict | None:
    """A Product node, searching plain lists and @graph arrays."""
    if isinstance(node, list):
        for item in node:
            found = _find_product(item)
            if found is not None:
                return found
        return None
    if not isinstance(node, dict):
        return None
    if node.get("@type") == "Product" and node.get("name"):
        return node
    for item in node.get("@graph") or []:
        found = _find_product(item)
        if found is not None:
            return found
    return None


def _title(product: dict | None, r: Rendered) -> str | None:
    """Product name from the JSON-LD node, falling back to the <h1> then the page title."""
    if product is not None:
        name = product.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", r.html, re.IGNORECASE | re.DOTALL)
    if m:
        text = _strip_tags(m.group(1))
        if text:
            return text
    return r.title


def _price(product: dict | None) -> str | None:
    """First real offer price formatted with its currency, else None."""
    if product is None:
        return None
    for price, currency in _offers(product):
        text = _format_price(price, currency)
        if text is not None:
            return text
    return None


def _offers(product: dict) -> list[tuple[object, str]]:
    """(price, currency) pairs from the offers dict or a list of offers."""
    offers = product.get("offers")
    items = offers if isinstance(offers, list) else [offers]
    return [
        (offer.get("price"), str(offer.get("priceCurrency") or "USD"))
        for offer in items
        if isinstance(offer, dict)
    ]


def _format_price(price: object, currency: str) -> str | None:
    """A display price (e.g. $375.00), else None for a missing or non-positive price."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def _rating(product: dict | None) -> tuple[str | None, str | None]:
    """(star rating text, reported-count text) from aggregateRating, else (None, None)."""
    if product is None:
        return None, None
    agg = product.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    return _star_text(agg.get("ratingValue")), _count_text(agg.get("reviewCount") or agg.get("ratingCount"))


def _star_text(value: object) -> str | None:
    """'4.6 out of 5 stars' for a valid rating value, else None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 5:
        return None
    return f"{v:g} out of 5 stars"


def _count_text(value: object) -> str | None:
    """'2,645 ratings' for a valid count, else None."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{n:,} ratings"


def _reported_review_count(product: dict | None) -> int | None:
    """Home Depot's total review count, or None when it is missing or invalid."""
    if product is None:
        return None
    agg = product.get("aggregateRating")
    if not isinstance(agg, dict):
        return None
    raw = agg.get("reviewCount") or agg.get("ratingCount")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _brand(product: dict | None) -> str | None:
    """Brand name from the Product node's brand object (or a plain string brand)."""
    if product is None:
        return None
    brand = product.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def _description(product: dict | None) -> str | None:
    """Product description text from the Product node, tags stripped."""
    if product is None:
        return None
    desc = product.get("description")
    if not isinstance(desc, str):
        return None
    return _strip_tags(desc)


def _details(product: dict | None) -> list[str]:
    """Label: value lines for the known spec fields, in display order."""
    if product is None:
        return []
    out: list[str] = []
    for key, label in _DETAIL_FIELDS:
        value = product.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            out.append(f"{label}: {value}")
    return out


def _reviews_heading(limit: int) -> str:
    """A readable heading that also marks the indexed review limit."""
    return f"## Home Depot reviews (up to {limit})"


def _reviews(product: dict | None, limit: int) -> list[str]:
    """Review entries from the Product node's `review` array, capped at limit."""
    if product is None:
        return []
    reviews = product.get("review")
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
        headline = review.get("headline")
        if isinstance(headline, str) and headline.strip():
            block.append(f"**{headline.strip()}**")
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


register(HomeDepotExtractor(), "homedepot.com")
