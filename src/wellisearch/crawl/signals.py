"""Quality-gate signal helpers: price / stock detection (design §3.2)."""
from __future__ import annotations

import re

PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})")


def find_price(html: str) -> str | None:
    """First price in the html (spaces stripped), else None."""
    m = PRICE_RE.search(html)
    if m is None:
        return None
    return m.group(0).replace(" ", "")


def find_stock(html: str) -> str | None:
    """Stock signal: 'out of stock' | 'in stock' | 'only N left' | None."""
    low = html.lower()
    if "out of stock" in low:
        return "out of stock"
    if "in stock" in low:
        return "in stock"
    m = re.search(r"only (\d+) left", low)
    if m is not None:
        return f"only {m.group(1)} left"
    return None
