"""BestBuyExtractor: hybrid fit-markdown + JSON-LD price fallback (design §3.2)."""
from __future__ import annotations

import json

from ..results import Fitted, Rendered
from ..signals import find_price, find_stock
from . import register
from .base import MIN_PRODUCT_CHARS, generic_md, trim_md


class BestBuyExtractor:
    """BestBuy product page: price from the page, else JSON-LD offers."""

    name = "bestbuy"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown with price (visible, else JSON-LD) + stock signals."""
        price = find_price(r.html)
        if price is None:
            price = _jsonld_price(r.html)
        return Fitted(
            md=trim_md(generic_md(r.html)),
            title=r.title,
            signals={"price": price, "stock": find_stock(r.html)},
            flags={"extractor": "bestbuy"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a price signal and real product content (not a stub)."""
        return bool(f.signals.get("price")) and len(f.md.strip()) >= MIN_PRODUCT_CHARS


def _jsonld_price(html: str) -> str | None:
    """Product offers.price from JSON-LD script blocks, else None."""
    try:
        from lxml import html as lxml_html

        scripts = lxml_html.fromstring(html).xpath('//script[@type="application/ld+json"]')
    except Exception:
        return None
    for script in scripts:
        if not script.text:
            continue
        try:
            data = json.loads(script.text)
        except Exception:
            continue
        price = _product_price(data)
        if price is not None:
            return price
    return None


def _product_price(data: object) -> str | None:
    """offers.price for a @type Product node (lists, @graph, offer lists)."""
    if isinstance(data, list):
        return _first_product_price(data)
    if not isinstance(data, dict):
        return None
    if data.get("@type") == "Product":
        price = _offers_price(data.get("offers"))
        if price is not None:
            return price
    for item in data.get("@graph") or []:
        price = _product_price(item)
        if price is not None:
            return price
    return None


def _offers_price(offers: object) -> str | None:
    """price from a single offer dict or a list of offers, else None."""
    if isinstance(offers, dict):
        price = offers.get("price")
        return str(price) if price is not None else None
    if isinstance(offers, list):
        return _first_offer_price(offers)
    return None


def _first_product_price(items: object) -> str | None:
    """First non-None recursive product price among items, else None."""
    for item in items:
        price = _product_price(item)
        if price is not None:
            return price
    return None


def _first_offer_price(offers: object) -> str | None:
    """First non-None price among dict offers, else None."""
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price")
        if price is not None:
            return str(price)
    return None


register(BestBuyExtractor(), "bestbuy.com")
