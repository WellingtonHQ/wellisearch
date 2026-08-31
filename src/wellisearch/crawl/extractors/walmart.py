"""WalmartExtractor: hybrid fit-markdown + hero-price gate (design §3.2)."""
from __future__ import annotations

from ..results import Fitted, Rendered
from ..signals import find_price, find_stock
from . import register
from .base import MIN_PRODUCT_CHARS, generic_md, trim_md


class WalmartExtractor:
    """Walmart product page: gate on the price signal."""

    name = "walmart"

    def fit(self, r: Rendered) -> Fitted:
        """Hybrid markdown with price/stock signals."""
        return Fitted(
            md=trim_md(generic_md(r.html)),
            title=r.title,
            signals={"price": find_price(r.html), "stock": find_stock(r.html)},
            flags={"extractor": "walmart"},
        )

    def accept(self, f: Fitted) -> bool:
        """Gate: a price signal and real product content (not a stub)."""
        return bool(f.signals.get("price")) and len(f.md.strip()) >= MIN_PRODUCT_CHARS


register(WalmartExtractor(), "walmart.com")
